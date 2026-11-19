"""Cache-Aside sobre Redis con degradación a caché local LRU.

Claves del contrato:
  rec:{user_id}  -> Top-10 serializado (TTL 3600 s)
  top:global     -> Top-10 Most Popular (fallback)
  sim:svd_failure -> bandera de demo para forzar el Circuit Breaker
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

import redis
from redis.exceptions import RedisError

from app.config import settings
from app.logging_utils import get_logger

logger = get_logger("fastsense.cache")


class CacheClient:
    def __init__(self, url: Optional[str] = None) -> None:
        self._client = redis.Redis.from_url(
            url or settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
            health_check_interval=30,
            retry_on_timeout=False,
        )
        self._lock = threading.RLock()
        self._local_recs: "OrderedDict[str, tuple[float, list[dict[str, Any]]]]" = OrderedDict()
        self._local_top: list[dict[str, Any]] = []
        self._local_sim = {"enabled": False, "latency_ms": 0}
        self._local_lru_size = 512
        self._last_error_log = 0.0

    # ------------------------------------------------------------------
    # Infraestructura
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except RedisError:
            return False

    def _warn(self, operation: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_log > 15:
            self._last_error_log = now
            logger.warning("redis_error op=%s error=%s", operation, type(exc).__name__)

    # ------------------------------------------------------------------
    # Recomendaciones
    # ------------------------------------------------------------------
    def get_recommendation(self, user_id: str) -> Optional[list[dict[str, Any]]]:
        key = f"rec:{user_id}"
        try:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        except (RedisError, json.JSONDecodeError) as exc:
            self._warn("get_recommendation", exc)

        with self._lock:
            entry = self._local_recs.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._local_recs.pop(key, None)
                return None
            self._local_recs.move_to_end(key)
            return value

    def set_recommendation(
        self,
        user_id: str,
        items: list[dict[str, Any]],
        ttl: Optional[int] = None,
    ) -> None:
        key = f"rec:{user_id}"
        ttl = ttl or settings.cache_ttl_seconds
        payload = json.dumps(items, ensure_ascii=False, default=str)
        try:
            self._client.setex(key, ttl, payload)
        except RedisError as exc:
            self._warn("set_recommendation", exc)

        with self._lock:
            self._local_recs[key] = (time.time() + ttl, items)
            self._local_recs.move_to_end(key)
            while len(self._local_recs) > self._local_lru_size:
                self._local_recs.popitem(last=False)

    def delete_recommendation(self, user_id: str) -> None:
        try:
            self._client.delete(f"rec:{user_id}")
        except RedisError as exc:
            self._warn("delete_recommendation", exc)
        with self._lock:
            self._local_recs.pop(f"rec:{user_id}", None)

    def clear_recommendations(self) -> int:
        deleted = 0
        try:
            for key in self._client.scan_iter(match="rec:*", count=500):
                self._client.delete(key)
                deleted += 1
        except RedisError as exc:
            self._warn("clear_recommendations", exc)
        with self._lock:
            deleted += len(self._local_recs)
            self._local_recs.clear()
        return deleted

    # ------------------------------------------------------------------
    # Ranking global (fallback)
    # ------------------------------------------------------------------
    def get_top_global(self) -> list[dict[str, Any]]:
        try:
            raw = self._client.get("top:global")
            if raw:
                return json.loads(raw)
        except (RedisError, json.JSONDecodeError) as exc:
            self._warn("get_top_global", exc)
        with self._lock:
            return list(self._local_top)

    def set_top_global(self, items: list[dict[str, Any]]) -> None:
        payload = json.dumps(items, ensure_ascii=False, default=str)
        try:
            self._client.set("top:global", payload)
        except RedisError as exc:
            self._warn("set_top_global", exc)
        with self._lock:
            self._local_top = list(items)

    # ------------------------------------------------------------------
    # Simulación de falla para la demo en vivo
    # ------------------------------------------------------------------
    def set_simulation(self, enabled: bool, latency_ms: int = 0) -> dict[str, Any]:
        state = {"enabled": bool(enabled), "latency_ms": int(latency_ms)}
        try:
            self._client.set("sim:svd_failure", json.dumps(state), ex=3600)
        except RedisError as exc:
            self._warn("set_simulation", exc)
        with self._lock:
            self._local_sim = state
        return state

    def get_simulation(self) -> dict[str, Any]:
        try:
            raw = self._client.get("sim:svd_failure")
            if raw:
                return json.loads(raw)
        except (RedisError, json.JSONDecodeError) as exc:
            self._warn("get_simulation", exc)
        with self._lock:
            return dict(self._local_sim)

    # ------------------------------------------------------------------
    # Contadores operativos (compartidos por las réplicas)
    # ------------------------------------------------------------------
    def incr(self, key: str, amount: int = 1) -> None:
        try:
            self._client.incrby(key, amount)
        except RedisError as exc:
            self._warn("incr", exc)

    def get_int(self, key: str) -> int:
        try:
            value = self._client.get(key)
            return int(value) if value is not None else 0
        except (RedisError, ValueError):
            return 0


cache = CacheClient()
