"""FastSense · API de inferencia.

Endpoints del contrato arc42 capítulo 5.4:
  GET  /recommendations/{user_id}
  POST /track-interaction
  GET  /health
  GET  /ready
  POST /internal/reload-model
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

from app import db
from app.auth import AuthMiddleware, require_internal_token
from app.aws import get_queue_depth
from app.cache import cache
from app.catalog import catalog
from app.circuit_breaker import SVDService
from app.config import settings
from app.events import publish_with_retry
from app.logging_utils import (
    anonymize_identifier,
    configure_logging,
    get_logger,
    hash_pii,
    redact_path,
)
from app.metrics import metrics
from app.model_loader import model_loader
from app.models import (
    HealthResponse,
    ReadyResponse,
    RecommendationResponse,
    TrackInteractionRequest,
    TrackInteractionResponse,
)
from app.recommender import RecommendationService

configure_logging()
logger = get_logger("fastsense.api")


# =====================================================================
# Middleware de trazabilidad + logs JSON
# =====================================================================
class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id[:80]
        started = time.perf_counter()
        status_code = 500
        response: Optional[StarletteResponse] = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            logger.exception(
                "request_unhandled path=%s method=%s",
                redact_path(request.url.path),
                request.method,
                extra={
                    "request_id": request.state.request_id,
                    "status_code": 500,
                    "path": redact_path(request.url.path),
                    "method": request.method,
                    "source": getattr(request.state, "source", None),
                },
            )
            raise
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            source = getattr(request.state, "source", None)
            user_id = getattr(request.state, "user_id", None)
            if response is not None:
                response.headers["X-Request-ID"] = request.state.request_id
                response.headers["X-Response-Time-Ms"] = f"{latency_ms:.2f}"

            path = request.url.path
            if path.startswith("/recommendations"):
                metrics.record(source or "unknown", latency_ms, status_code)
            elif path.startswith("/track-interaction"):
                cache.incr("metrics:tracking_requests")

            logger.info(
                "request",
                extra={
                    "request_id": request.state.request_id,
                    "user_id": anonymize_identifier(user_id),
                    "latency_ms": round(latency_ms, 3),
                    "source": source or "none",
                    "status_code": status_code,
                    "path": redact_path(path),
                    "method": request.method,
                },
            )


# =====================================================================
# Aplicación
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = time.monotonic()
    app.state.svd = SVDService(model_loader)
    app.state.recommender = RecommendationService(app.state.svd)
    app.state.queue_lock = threading.Lock()
    app.state.queue_cache = {"at": 0.0, "queue": -1, "dlq": -1}
    app.state.users_lock = threading.Lock()
    app.state.users_cache = {"at": 0.0, "items": []}

    catalog.start()
    model_loader.start()
    logger.info(
        "startup_complete version=%s model_loaded=%s timeout_ms=%d",
        settings.app_version,
        model_loader.model_loaded,
        settings.svd_timeout_ms,
    )
    try:
        yield
    finally:
        model_loader.stop()
        catalog.stop()
        app.state.svd.shutdown()
        logger.info("shutdown_complete")


app = FastAPI(
    title="FastSense · Recomendador ShopFast",
    version=settings.app_version,
    description=(
        "API de inferencia con SVD (scikit-surprise), Cache-Aside Redis, "
        "Circuit Breaker de 400 ms y degradación elegante a Top-10 Most Popular. "
        "Autenticación: X-API-Key + Authorization: Bearer <JWT HS256>."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
)

# El último middleware añadido es el más externo:
#   CORS -> RequestContext (logs/request-id) -> Auth -> Router
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "X-Request-ID",
        "X-Response-Time-Ms",
        "X-Recommendation-Source",
        "X-Model-Version",
        "X-Event-ID",
    ],
    max_age=600,
)


# =====================================================================
# Helpers
# =====================================================================
def _uptime(request: Request) -> float:
    return round(time.monotonic() - getattr(request.app.state, "started_at", time.monotonic()), 3)


def _queue_depths(request: Request) -> tuple[int, int]:
    state = request.app.state
    now = time.monotonic()
    with state.queue_lock:
        if now - state.queue_cache["at"] < 3:
            return state.queue_cache["queue"], state.queue_cache["dlq"]
    queue_depth = get_queue_depth()
    dlq_depth = get_queue_depth(settings.sqs_dlq_url) if settings.sqs_dlq_url else -1
    with state.queue_lock:
        state.queue_cache = {"at": now, "queue": queue_depth, "dlq": dlq_depth}
    return queue_depth, dlq_depth


def _demo_users(request: Request, limit: int) -> list[dict[str, Any]]:
    state = request.app.state
    now = time.monotonic()
    with state.users_lock:
        if now - state.users_cache["at"] < 60 and state.users_cache["items"]:
            return state.users_cache["items"][:limit]
    try:
        users = db.get_demo_users(max(limit, 50))
    except Exception:
        users = []
    if users:
        with state.users_lock:
            state.users_cache = {"at": now, "items": users}
    return users[:limit]


# =====================================================================
# Observabilidad
# =====================================================================
@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
    }


@app.get("/health", response_model=HealthResponse, tags=["observability"])
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        uptime_s=_uptime(request),
        service=settings.app_name,
    )


@app.get("/ready", response_model=ReadyResponse, tags=["observability"])
def ready(request: Request):
    redis_ok = cache.ping()
    db_ok = db.check_db()
    loaded = model_loader.model_loaded
    is_ready = redis_ok and db_ok and loaded
    payload = ReadyResponse(
        status="ready" if is_ready else "not_ready",
        redis="ok" if redis_ok else "error",
        rds="ok" if db_ok else "error",
        model_loaded=loaded,
        model_version=model_loader.version,
        uptime_s=_uptime(request),
    )
    if not is_ready:
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return payload


# =====================================================================
# Recomendaciones
# =====================================================================
@app.get(
    "/recommendations/{user_id}",
    response_model=RecommendationResponse,
    tags=["recommendations"],
    responses={200: {"description": "Top-N con source=cache|svd|fallback"}},
)
def recommendations(
    user_id: str,
    request: Request,
    response: Response,
    limit: int = Query(settings.default_limit, ge=1, le=settings.max_limit),
    simulate_failure: bool = Query(False, description="Fuerza fallback para demo"),
    x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
) -> RecommendationResponse:
    if not user_id.strip() or len(user_id) > 64:
        raise HTTPException(status_code=400, detail="user_id inválido")

    request.state.user_id = user_id
    force = simulate_failure or str(x_simulate_failure).lower() in {"1", "true", "yes"}
    started = time.perf_counter()
    service: RecommendationService = request.app.state.recommender

    try:
        items, source = service.recommend(user_id, limit=limit, force_failure=force)
    except Exception as exc:  # defensa final: jamás 5xx al cliente
        logger.error("recommendation_emergency error=%s", type(exc).__name__)
        items, source = service.emergency_fallback(limit), "fallback"

    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    request.state.source = source
    response.headers["X-Recommendation-Source"] = source
    response.headers["X-Model-Version"] = model_loader.version
    response.headers["Cache-Control"] = "no-store"
    return RecommendationResponse(
        user_id=user_id,
        items=items,  # type: ignore[arg-type]
        source=source,
        request_id=request.state.request_id,
        latency_ms=latency_ms,
    )


# =====================================================================
# Tracking asíncrono
# =====================================================================
@app.post(
    "/track-interaction",
    response_model=TrackInteractionResponse,
    status_code=202,
    tags=["interactions"],
)
def track_interaction(
    payload: TrackInteractionRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> TrackInteractionResponse:
    request.state.user_id = payload.user_id
    event_id = f"evt_{uuid.uuid4().hex}"
    accepted_at = datetime.now(timezone.utc)

    timestamp = payload.timestamp or accepted_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    message: dict[str, Any] = {
        "event_id": event_id,
        "idempotency_key": event_id,
        "user_id": payload.user_id,
        "product_id": payload.product_id,
        "event_type": payload.event_type,
        "rating": payload.rating,
        "session_id": payload.session_id or f"s_{uuid.uuid4().hex[:12]}",
        "timestamp": timestamp.isoformat(),
        "accepted_at": accepted_at.isoformat(),
        "request_id": request.state.request_id,
    }
    # El correo nunca viaja en claro: solo su hash SHA-256.
    if payload.email:
        message["email_hash"] = hash_pii(payload.email)

    background_tasks.add_task(publish_with_retry, message)

    response.headers["X-Event-ID"] = event_id
    response.headers["X-Response-Time-Ms"] = "0"
    return TrackInteractionResponse(
        event_id=event_id, status="accepted", accepted_at=accepted_at
    )


# =====================================================================
# Dashboard MLOps / negocio
# =====================================================================
@app.get("/metrics/summary", tags=["observability"])
def metrics_summary(request: Request) -> dict[str, Any]:
    snapshot = metrics.snapshot()
    model_status = model_loader.status()
    db_model = db.get_active_model()
    queue_depth, dlq_depth = _queue_depths(request)
    try:
        ingested = db.count_interactions()
    except Exception:
        ingested = -1

    model_payload = {
        "version": model_status.get("version") if model_status.get("model_loaded") else None,
        "rmse": model_status.get("rmse"),
        "precision_at_10": model_status.get("precision_at_10"),
        "trained_at": model_status.get("trained_at"),
        "s3_uri": model_status.get("s3_uri"),
        "model_loaded": model_status.get("model_loaded"),
        "n_users": model_status.get("n_users"),
        "n_items": model_status.get("n_items"),
        "registry": db_model,
        "gate_rmse_max": 0.85,
    }
    if db_model and not model_status.get("model_loaded"):
        model_payload.update(
            {
                "version": db_model["version"],
                "rmse": db_model["rmse"],
                "precision_at_10": db_model["precision_at_10"],
                "trained_at": db_model["trained_at"],
                "s3_uri": db_model["s3_uri"],
            }
        )

    uptime_s = _uptime(request)
    total = snapshot["sources"]["total"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_s": uptime_s,
        "model": model_payload,
        "golden_signals": {
            "latency": snapshot["latency"],
            "traffic": {
                "requests_total": total,
                "throughput_rps": round(total / uptime_s, 3) if uptime_s else 0.0,
            },
            "errors": {
                "count": snapshot["errors"],
                "rate": snapshot["error_rate"],
            },
            "saturation": {
                "cache_connected": cache.ping(),
                "queue_depth": queue_depth,
                "dlq_depth": dlq_depth,
            },
        },
        "sources": snapshot["sources"],
        "cache_hit_rate": snapshot["cache_hit_rate"],
        "events": {
            "ingested": ingested,
            "queue_depth": queue_depth,
            "dlq_depth": dlq_depth,
        },
        "circuit_breaker": request.app.state.svd.state(),
        "simulation": cache.get_simulation(),
        "tracking": {
            "accepted": cache.get_int("metrics:tracking_accepted"),
            "failed": cache.get_int("metrics:tracking_failed"),
            "requests": cache.get_int("metrics:tracking_requests"),
        },
    }


@app.get("/users", tags=["catalog"])
def users(request: Request, limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
    return {"users": _demo_users(request, limit)}


@app.get("/catalog", tags=["catalog"])
def read_catalog(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = None,
) -> dict[str, Any]:
    items = catalog.list_products(limit=limit, offset=offset, category=category)
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


@app.get("/products/{product_id}", tags=["catalog"])
def product_detail(product_id: str) -> dict[str, Any]:
    product = catalog.get(product_id) or db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="producto no encontrado")
    return product


# =====================================================================
# Endpoints internos (MLOps / demo)
# =====================================================================
@app.post("/internal/reload-model", tags=["internal"], dependencies=[Depends(require_internal_token)])
def reload_model() -> dict[str, Any]:
    loaded = model_loader.reload(force=True)
    # Al promover un modelo nuevo el catálogo puede haberse sembrado después
    # del arranque; refrescarlo evita nombres/atributos incompletos.
    catalog.refresh()
    catalog.bootstrap_popular()
    status = model_loader.status()
    if not loaded and not status.get("model_loaded"):
        return JSONResponse(status_code=503, content={"reloaded": False, **status})
    return {"reloaded": loaded, **status}


@app.post("/internal/simulate-failure", tags=["internal"], dependencies=[Depends(require_internal_token)])
def simulate_failure(
    request: Request,
    enabled: bool = Query(True),
    latency_ms: int = Query(0, ge=0, le=5000),
) -> dict[str, Any]:
    state = cache.set_simulation(enabled=enabled, latency_ms=latency_ms)
    if enabled:
        request.app.state.svd.open_breaker()
    else:
        request.app.state.svd.reset_breaker()
    logger.warning(
        "simulation_changed enabled=%s latency_ms=%d", enabled, latency_ms
    )
    return {
        "simulation": state,
        "circuit_breaker": request.app.state.svd.state(),
        "message": (
            "SVD degradado a fallback (top:global)"
            if enabled
            else "SVD restaurado"
        ),
    }


@app.post("/internal/reset-circuit-breaker", tags=["internal"], dependencies=[Depends(require_internal_token)])
def reset_circuit_breaker(request: Request) -> dict[str, Any]:
    request.app.state.svd.reset_breaker()
    return {"circuit_breaker": request.app.state.svd.state(), "simulation": cache.get_simulation()}


@app.get("/internal/model-status", tags=["internal"], dependencies=[Depends(require_internal_token)])
def model_status(request: Request) -> dict[str, Any]:
    return {
        "loader": model_loader.status(),
        "registry": db.get_active_model(),
        "circuit_breaker": request.app.state.svd.state(),
    }
