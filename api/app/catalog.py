"""Catálogo en memoria para enriquecer recomendaciones sin tocar MySQL en el
camino caliente. Se refresca cada 60 s en un hilo de fondo.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from app import db
from app.cache import cache
from app.logging_utils import get_logger

logger = get_logger("fastsense.catalog")


class CatalogRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._popular: list[dict[str, Any]] = []
        self._loaded_at = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def refresh(self) -> None:
        try:
            items = db.get_catalog()
        except Exception as exc:
            logger.warning("catalog_refresh_failed error=%s", type(exc).__name__)
            return
        if not items:
            return
        with self._lock:
            self._items = {item["product_id"]: item for item in items}
            self._loaded_at = time.time()
        logger.info("catalog_loaded products=%d", len(items))

    def bootstrap_popular(self) -> None:
        """Garantiza top:global aun antes de que termine el entrenamiento."""
        existing = cache.get_top_global()
        if existing:
            with self._lock:
                self._popular = list(existing)
            return
        try:
            popular = db.get_popular_products(20)
        except Exception as exc:
            logger.warning("popular_bootstrap_failed error=%s", type(exc).__name__)
            return
        if popular:
            with self._lock:
                self._popular = list(popular)
            cache.set_top_global(popular)
            logger.info("top_global_bootstrapped items=%d", len(popular))

    def set_popular(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self._popular = list(items)
        cache.set_top_global(items)

    def get(self, product_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._items.get(product_id)

    def popular(self, limit: int = 10) -> list[dict[str, Any]]:
        items = cache.get_top_global()
        if not items:
            with self._lock:
                items = list(self._popular)
        if not items:
            try:
                items = db.get_popular_products(max(limit, 10))
            except Exception:
                items = []
            if items:
                self.set_popular(items)
        return items[:limit]

    def enrich(self, product_id: str, score: float, rank: int) -> dict[str, Any]:
        product = self.get(product_id)
        if not product:
            # El catálogo puede haberse cargado antes de la siembra: rellena
            # perezosamente para no mostrar nombres incompletos en la demo.
            try:
                product = db.get_product(product_id)
            except Exception:
                product = None
            if product:
                with self._lock:
                    self._items[product_id] = product
        if product:
            return {
                "product_id": product["product_id"],
                "name": product["name"],
                "category": product["category"],
                "price": float(product["price"]),
                "score": round(float(score), 4),
                "rank": rank,
            }
        return {
            "product_id": product_id,
            "name": product_id,
            "category": "unknown",
            "price": 0.0,
            "score": round(float(score), 4),
            "rank": rank,
        }

    def list_products(
        self, limit: int = 50, offset: int = 0, category: Optional[str] = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._items.values())
        if category:
            values = [item for item in values if item["category"] == category]
        return values[offset : offset + limit]

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(20)
            if not self._stop.is_set():
                self.refresh()

    def start(self) -> None:
        self.refresh()
        self.bootstrap_popular()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, name="catalog-refresher", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


catalog = CatalogRepository()
