"""Golden Signals compartidas entre réplicas usando Redis.

Si Redis cae, se conserva un resumen en memoria para no perder el dashboard.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

import numpy as np
from redis.exceptions import RedisError

from app.cache import cache
from app.logging_utils import get_logger

logger = get_logger("fastsense.metrics")

LATENCY_KEY = "metrics:latency"
SOURCE_KEY = "metrics:sources"
ERROR_KEY = "metrics:errors"
TOTAL_KEY = "metrics:requests"
MAX_SAMPLES = 2000


class MetricsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local_latencies: deque[float] = deque(maxlen=MAX_SAMPLES)
        self._local_sources: Counter[str] = Counter()
        self._local_errors = 0
        self._local_total = 0

    def record(self, source: str, latency_ms: float, status_code: int) -> None:
        source = source or "unknown"
        with self._lock:
            self._local_latencies.append(float(latency_ms))
            self._local_sources[source] += 1
            self._local_total += 1
            if status_code >= 500:
                self._local_errors += 1
        try:
            client = cache._client  # cliente compartido
            pipe = client.pipeline()
            pipe.lpush(LATENCY_KEY, f"{latency_ms:.3f}")
            pipe.ltrim(LATENCY_KEY, 0, MAX_SAMPLES - 1)
            pipe.hincrby(SOURCE_KEY, source, 1)
            pipe.hincrby(SOURCE_KEY, "total", 1)
            pipe.incr(TOTAL_KEY)
            if status_code >= 500:
                pipe.incr(ERROR_KEY)
            pipe.execute()
        except RedisError as exc:
            logger.warning("metrics_redis_error error=%s", type(exc).__name__)

    def _read_redis_latencies(self) -> list[float]:
        try:
            raw = cache._client.lrange(LATENCY_KEY, 0, MAX_SAMPLES - 1)
            return [float(value) for value in raw if value]
        except (RedisError, ValueError):
            return []

    def snapshot(self) -> dict[str, Any]:
        latencies = self._read_redis_latencies()
        if not latencies:
            with self._lock:
                latencies = list(self._local_latencies)

        if latencies:
            array = np.asarray(latencies, dtype=float)
            p50, p95, p99 = (float(np.percentile(array, q)) for q in (50, 95, 99))
        else:
            p50 = p95 = p99 = 0.0

        sources: dict[str, int] = {}
        total = 0
        errors = 0
        try:
            raw_sources = cache._client.hgetall(SOURCE_KEY)
            sources = {key: int(value) for key, value in raw_sources.items() if key != "total"}
            total = int(raw_sources.get("total", sum(sources.values()) or 0))
            errors = int(cache._client.get(ERROR_KEY) or 0)
        except (RedisError, ValueError):
            with self._lock:
                sources = dict(self._local_sources)
                total = self._local_total
                errors = self._local_errors

        if not sources:
            with self._lock:
                sources = dict(self._local_sources)
                total = self._local_total
                errors = self._local_errors

        cache_hits = sources.get("cache", 0)
        cache_hit_rate = (cache_hits / total) if total else 0.0
        return {
            "latency": {
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "p99_ms": round(p99, 2),
                "samples": len(latencies),
            },
            "sources": {
                "cache": sources.get("cache", 0),
                "svd": sources.get("svd", 0),
                "fallback": sources.get("fallback", 0),
                "total": total,
            },
            "cache_hit_rate": round(cache_hit_rate, 4),
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
        }


metrics = MetricsCollector()
