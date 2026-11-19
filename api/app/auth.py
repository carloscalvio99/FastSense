"""Autenticación por API Key + JWT HS256.

Endpoints públicos: /health, /ready, /docs, /openapi.json y /redoc.
Los endpoints /internal/* usan X-Internal-Token y se validan en el router.
"""

from __future__ import annotations

import hmac
from typing import Callable

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import settings
from app.logging_utils import get_logger, redact_path

logger = get_logger("fastsense.auth")

PUBLIC_PATHS = {
    "/",
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/docs/", "/redoc/", "/internal/", "/static/")


def _unauthorized(detail: str, www_authenticate: str = 'Bearer realm="fastsense"') -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": www_authenticate},
    )


def validate_jwt(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"require": ["exp", "sub"]},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key", "")
        if not api_key or not hmac.compare_digest(api_key, settings.api_key):
            logger.warning(
                "auth_failed reason=invalid_api_key path=%s",
                redact_path(path),
                extra={"request_id": request.headers.get("X-Request-ID", "unknown")},
            )
            return _unauthorized("API Key inválida o ausente (X-API-Key)")

        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _unauthorized("Token JWT ausente (Authorization: Bearer <JWT>)")

        try:
            claims = validate_jwt(token)
        except jwt.ExpiredSignatureError:
            return _unauthorized("Token JWT expirado")
        except jwt.InvalidTokenError as exc:
            logger.warning(
                "auth_failed reason=invalid_jwt path=%s error=%s",
                redact_path(path),
                type(exc).__name__,
            )
            return _unauthorized("Token JWT inválido")

        request.state.jwt_subject = claims.get("sub")
        request.state.jwt_claims = claims
        return await call_next(request)


def require_internal_token(request: Request) -> None:
    """Dependencia para endpoints internos (demo MLOps / reload)."""
    from fastapi import HTTPException

    token = request.headers.get("X-Internal-Token", "")
    if not token or not hmac.compare_digest(token, settings.internal_token):
        raise HTTPException(status_code=401, detail="X-Internal-Token inválido o ausente")
