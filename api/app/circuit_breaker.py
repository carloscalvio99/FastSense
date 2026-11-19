"""Circuit Breaker (pybreaker) + timeout duro de 400 ms para el motor SVD.

Regla: si el SVD excede el presupuesto, falla o el circuito está abierto,
la petición se degrada al Top-10 global. Nunca se propaga un 5xx.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Optional

import pybreaker

from app.cache import cache
from app.config import settings
from app.logging_utils import anonymize_identifier, get_logger
from app.model_loader import ModelLoader, ModelNotLoaded, UnknownUser

logger = get_logger("fastsense.circuit_breaker")


class SVDTimeout(RuntimeError):
    pass


class SVDUnavailable(RuntimeError):
    pass


class _BreakerListener(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state, *args, **kwargs) -> None:  # noqa: ANN001
        logger.warning(
            "circuit_breaker_state_change old=%s new=%s fail_counter=%s",
            getattr(old_state, "name", old_state),
            getattr(new_state, "name", new_state),
            getattr(cb, "fail_counter", "?"),
        )

    def failure(self, cb, exc, *args, **kwargs) -> None:  # noqa: ANN001
        logger.warning(
            "circuit_breaker_failure error=%s fail_counter=%s",
            type(exc).__name__,
            getattr(cb, "fail_counter", "?"),
        )


class SVDService:
    def __init__(self, loader: ModelLoader) -> None:
        self.loader = loader
        self._executor = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="svd-infer"
        )
        self.breaker = pybreaker.CircuitBreaker(
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_s,
            exclude=[UnknownUser, ModelNotLoaded],
            listeners=[_BreakerListener()],
            name="svd-predictor",
        )

    # ------------------------------------------------------------------
    def _guarded_predict(
        self,
        user_id: str,
        limit: int,
        latency_ms: int = 0,
    ) -> list[tuple[str, float]]:
        """Timeout estricto: se ejecuta en un hilo y se espera como máximo 400 ms."""
        # Se captura el snapshot: el hilo usa una versión inmutable del modelo
        # aunque el watcher publique una nueva mientras se calcula el score.
        snapshot = self.loader.snapshot
        if snapshot is None:
            raise ModelNotLoaded("model.pkl no está cargado")

        def _work():
            if latency_ms > 0:
                time.sleep(latency_ms / 1000.0)
            return snapshot.predict(user_id, limit)

        future = self._executor.submit(_work)
        try:
            return future.result(timeout=settings.svd_timeout_seconds)
        except FuturesTimeoutError as exc:
            raise SVDTimeout(
                f"SVD excedió {settings.svd_timeout_ms} ms"
            ) from exc

    def predict(
        self,
        user_id: str,
        limit: int = 10,
        force_failure: bool = False,
    ) -> list[tuple[str, float]]:
        simulation = cache.get_simulation()
        simulated_failure = bool(simulation.get("enabled"))
        simulated_latency = int(simulation.get("latency_ms") or 0)

        if force_failure:
            # Falla solicitada explícitamente por header: degrada SOLO esta
            # petición, sin abrir el breaker global de la réplica.
            raise SVDUnavailable("falla SVD simulada por X-Simulate-Failure")

        if simulated_failure:
            # Bandera global (Redis) para la demo MLOps: abre el circuito en
            # todas las réplicas y fuerza el fallback.
            self.breaker.open()
            raise SVDUnavailable("falla SVD simulada para demo MLOps")

        return self.breaker.call(
            self._guarded_predict, user_id, limit, simulated_latency
        )

    # ------------------------------------------------------------------
    def state(self) -> dict[str, object]:
        return {
            "state": getattr(self.breaker.state, "name", str(self.breaker.state)).lower(),
            "fail_counter": int(getattr(self.breaker, "fail_counter", 0) or 0),
            "fail_max": settings.circuit_breaker_fail_max,
            "reset_timeout_s": settings.circuit_breaker_reset_s,
            "timeout_ms": settings.svd_timeout_ms,
        }

    def open_breaker(self) -> None:
        self.breaker.open()

    def reset_breaker(self) -> None:
        self.breaker.close()
        cache.set_simulation(False, 0)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
