"""Acceso a MySQL (SQLAlchemy Core). Sin ORM para mantener el camino caliente."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings
from app.logging_utils import get_logger

logger = get_logger("fastsense.db")


def _build_engine() -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_recycle=1800,
        connect_args={"connect_timeout": 3},
        future=True,
    )


engine: Engine = _build_engine()


@contextmanager
def connection() -> Iterator[Any]:
    with engine.connect() as conn:
        yield conn


def check_db() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depende del entorno
        logger.warning("mysql_unavailable error=%s", type(exc).__name__)
        return False


def _row_to_product(row: Any) -> dict[str, Any]:
    attributes = row.get("attributes")
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except json.JSONDecodeError:
            attributes = {}
    return {
        "product_id": row["product_id"],
        "name": row["name"],
        "category": row["category"],
        "price": float(row["price"]),
        "stock": int(row.get("stock") or 0),
        "attributes": attributes or {},
    }


def get_catalog() -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT product_id, name, category, price, stock, attributes "
                "FROM PRODUCTO ORDER BY product_id"
            )
        ).mappings()
        return [_row_to_product(row) for row in rows]


def get_product(product_id: str) -> Optional[dict[str, Any]]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT product_id, name, category, price, stock, attributes "
                    "FROM PRODUCTO WHERE product_id = :pid"
                ),
                {"pid": product_id},
            )
            .mappings()
            .first()
        )
        return _row_to_product(row) if row else None


def get_popular_products(limit: int = 10) -> list[dict[str, Any]]:
    """Top global ponderado por intención de compra (fallback)."""
    query = text(
        """
        SELECT p.product_id, p.name, p.category, p.price, p.stock, p.attributes,
               SUM(CASE i.event_type
                       WHEN 'purchase' THEN 5.0
                       WHEN 'cart'     THEN 3.0
                       WHEN 'rating'   THEN GREATEST(COALESCE(i.rating_value, 3), 1)
                       ELSE 1.0 END) AS popularity
        FROM INTERACCION i
        JOIN PRODUCTO p ON p.product_id = i.product_id
        GROUP BY p.product_id, p.name, p.category, p.price, p.stock, p.attributes
        ORDER BY popularity DESC, p.product_id ASC
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"lim": int(limit)}).mappings().all()
    items: list[dict[str, Any]] = []
    max_pop = max((float(row["popularity"]) for row in rows), default=1.0) or 1.0
    for rank, row in enumerate(rows, start=1):
        product = _row_to_product(row)
        items.append(
            {
                "product_id": product["product_id"],
                "name": product["name"],
                "category": product["category"],
                "price": product["price"],
                "score": round(min(1.0, float(row["popularity"]) / max_pop), 4),
                "rank": rank,
            }
        )
    return items


def get_demo_users(limit: int = 50) -> list[dict[str, Any]]:
    # Se selecciona primero un subconjunto de usuarios y luego se cuenta su
    # historial con el índice (user_id, timestamp). Evita un GROUP BY sobre
    # las 180k interacciones en cada arranque en frío del dashboard.
    query = text(
        """
        WITH sample AS (
            SELECT user_id, segment
            FROM USUARIO
            ORDER BY user_id ASC
            LIMIT :lim
        )
        SELECT s.user_id, s.segment, COUNT(i.interaction_id) AS interactions
        FROM sample s
        LEFT JOIN INTERACCION i ON i.user_id = s.user_id
        GROUP BY s.user_id, s.segment
        ORDER BY s.user_id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"lim": int(limit)}).mappings().all()
    return [
        {
            "user_id": row["user_id"],
            "segment": row["segment"],
            "interactions": int(row["interactions"]),
        }
        for row in rows
    ]


def get_active_model() -> Optional[dict[str, Any]]:
    query = text(
        """
        SELECT model_id, version, rmse, precision_at_10, s3_uri, trained_at, status
        FROM MODELO_VERSION
        WHERE status = 'active'
        ORDER BY trained_at DESC, model_id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(query).mappings().first()
    if not row:
        return None
    trained_at = row["trained_at"]
    if isinstance(trained_at, datetime):
        trained_at = trained_at.replace(tzinfo=timezone.utc).isoformat()
    return {
        "model_id": int(row["model_id"]),
        "version": row["version"],
        "rmse": float(row["rmse"]),
        "precision_at_10": float(row["precision_at_10"]),
        "s3_uri": row["s3_uri"],
        "trained_at": trained_at,
        "status": row["status"],
    }


def count_interactions() -> int:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT COUNT(*) FROM INTERACCION")).scalar()
    return int(value or 0)


def count_users() -> int:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT COUNT(*) FROM USUARIO")).scalar()
    return int(value or 0)


def count_products() -> int:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT COUNT(*) FROM PRODUCTO")).scalar()
    return int(value or 0)
