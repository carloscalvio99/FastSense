"""Inference Service Manager: orquesta Cache-Aside -> Circuit Breaker/SVD ->
Fallback Handler. Es el único lugar que decide el campo `source`.
"""

from __future__ import annotations

from typing import Any, Literal

from app.cache import cache
from app.catalog import catalog
from app.circuit_breaker import SVDService
from app.logging_utils import anonymize_identifier, get_logger

logger = get_logger("fastsense.recommender")

Source = Literal["cache", "svd", "fallback"]


class RecommendationService:
    def __init__(self, svd: SVDService) -> None:
        self.svd = svd

    # ------------------------------------------------------------------
    def _fallback(self, limit: int) -> list[dict[str, Any]]:
        """Degradación elegante: top:global de Redis, memoria local o MySQL."""
        try:
            items = catalog.popular(limit)
        except Exception as exc:
            logger.error("fallback_failed error=%s", type(exc).__name__)
            items = []
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(items[:limit], start=1):
            normalized.append(
                {
                    "product_id": item["product_id"],
                    "name": item.get("name", item["product_id"]),
                    "category": item.get("category", "unknown"),
                    "price": float(item.get("price", 0.0)),
                    "score": float(item.get("score", 0.0)),
                    "rank": index,
                }
            )
        return normalized

    def _from_svd_items(
        self, raw: list[tuple[str, float]], limit: int
    ) -> list[dict[str, Any]]:
        enriched = [
            catalog.enrich(product_id, score, rank)
            for rank, (product_id, score) in enumerate(raw[:limit], start=1)
        ]
        if len(enriched) < limit:
            seen = {item["product_id"] for item in enriched}
            for item in self._fallback(limit):
                if item["product_id"] not in seen:
                    item["rank"] = len(enriched) + 1
                    enriched.append(item)
                if len(enriched) >= limit:
                    break
        return enriched[:limit]

    # ------------------------------------------------------------------
    def recommend(
        self,
        user_id: str,
        limit: int = 10,
        force_failure: bool = False,
    ) -> tuple[list[dict[str, Any]], Source]:
        simulation = cache.get_simulation()
        bypass_cache = force_failure or bool(simulation.get("enabled")) or int(
            simulation.get("latency_ms") or 0
        ) > 0

        if not bypass_cache:
            cached = cache.get_recommendation(user_id)
            if cached:
                return [dict(item) for item in cached[:limit]], "cache"

        try:
            raw = self.svd.predict(user_id, limit=max(limit, 10), force_failure=force_failure)
            cache_items = self._from_svd_items(raw, max(limit, 10))
            items = cache_items[:limit]
            if not items:
                raise RuntimeError("SVD no devolvió ítems")
            # Se cachea el Top-10 (contrato) con TTL 3600 s.
            cache.set_recommendation(user_id, cache_items)
            return items, "svd"
        except Exception as exc:
            logger.warning(
                "svd_degraded user=%s reason=%s -> fallback",
                anonymize_identifier(user_id),
                type(exc).__name__,
            )
            return self._fallback(limit), "fallback"

    def emergency_fallback(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            return self._fallback(limit)
        except Exception:
            return []
