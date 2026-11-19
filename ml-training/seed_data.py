"""Semilla reproducible del Data Warehouse de FastSense.

Genera 5.000 productos, 50.000 usuarios y 180.000 interacciones con una
estructura latente aprendible (sesgos de usuario/ítem + factores) para que
el SVD alcance un RMSE realista y verificable por debajo del quality gate.

Ids deterministas:
  - Productos: p_1001 .. p_6000   (incluye p_1042)
  - Usuarios:  u_1001 .. u_51000  (incluye u_1001)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import create_engine, text

SEED = int(os.getenv("SEED", "42"))
TOTAL_PRODUCTS = int(os.getenv("TOTAL_PRODUCTS", "5000"))
TOTAL_USERS = int(os.getenv("TOTAL_USERS", "50000"))
TOTAL_INTERACTIONS = int(os.getenv("TOTAL_INTERACTIONS", "180000"))
PII_SALT = os.getenv("PII_SALT", "fastsense-pii-salt-2026")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://fastsense:fastsense@mysql:3306/fastsense?charset=utf8mb4",
)

CATEGORIES = [
    "Laptops",
    "Smartphones",
    "Audio",
    "Wearables",
    "Gaming",
    "Monitores",
    "Almacenamiento",
    "Redes",
    "Accesorios",
    "Smart Home",
]
BRANDS = [
    "Aster",
    "Nimbus",
    "Vertex",
    "Krypton",
    "Orion",
    "Helix",
    "Quantum",
    "Lumen",
    "Titan",
    "Zephyr",
]
ADJECTIVES = ["Pro", "Max", "Air", "Ultra", "Lite", "Plus", "Neo", "Edge", "Core", "X"]
SEGMENTS = ["new", "regular", "vip", "churn"]
SINGULAR = {
    "Laptops": "Laptop",
    "Smartphones": "Smartphone",
    "Audio": "Audio",
    "Wearables": "Wearable",
    "Gaming": "Gaming",
    "Monitores": "Monitor",
    "Almacenamiento": "Almacenamiento",
    "Redes": "Red",
    "Accesorios": "Accesorio",
    "Smart Home": "Smart Home",
}
EVENT_WEIGHTS = {"click": 0.55, "cart": 0.20, "purchase": 0.13, "rating": 0.12}
EVENT_TARGET = {"click": 3.0, "cart": 4.2, "purchase": 4.9}

LATENT_DIM = 8
RNG = np.random.default_rng(SEED)


def log(message: str) -> None:
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO", "service": "ml-seed", "message": message}), flush=True)


def email_hash(user_id: str) -> str:
    # Hash determinista con salt: no se almacena el correo en claro.
    value = f"{user_id}@shopfast.cl".strip().lower()
    return hashlib.sha256(f"{PII_SALT}{value}".encode("utf-8")).hexdigest()


def build_products() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(TOTAL_PRODUCTS):
        number = 1001 + index
        category = CATEGORIES[index % len(CATEGORIES)]
        brand = BRANDS[(index * 7) % len(BRANDS)]
        adjective = ADJECTIVES[(index * 3) % len(ADJECTIVES)]
        price = round(float(np.clip(RNG.lognormal(mean=5.2, sigma=0.6), 5, 4500)), 2)
        rows.append(
            {
                "product_id": f"p_{number}",
                "name": f"{brand} {SINGULAR[category]} {adjective} {number}",
                "category": category,
                "price": price,
                "stock": int(RNG.integers(0, 500)),
                "attributes": json.dumps(
                    {
                        "brand": brand,
                        "warranty_months": int(RNG.choice([6, 12, 24, 36])),
                        "rating_seed": round(float(RNG.uniform(3.0, 5.0)), 2),
                    }
                ),
            }
        )
    return rows


def build_users() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    segment_probs = [0.20, 0.50, 0.20, 0.10]
    segments = RNG.choice(SEGMENTS, size=TOTAL_USERS, p=segment_probs)
    now = datetime.now(timezone.utc)
    for index in range(TOTAL_USERS):
        number = 1001 + index
        user_id = f"u_{number}"
        created = now - timedelta(days=int(RNG.integers(0, 900)))
        rows.append(
            {
                "user_id": user_id,
                "email_hash": email_hash(user_id),
                "created_at": created.replace(tzinfo=None),
                "segment": str(segments[index]),
            }
        )
    return rows


def pick_event(latent_score: float, draw: float) -> str:
    """Convierte preferencia latente en tipo de evento observable."""
    if latent_score >= 4.30:
        if draw < 0.12:
            return "rating"
        return "purchase" if draw < 0.62 else "cart"
    if latent_score >= 3.60:
        if draw < 0.22:
            return "rating"
        if draw < 0.48:
            return "cart"
        return "click"
    if latent_score < 2.60:
        return "rating" if draw < 0.10 else "click"
    return "rating" if draw < 0.18 else "click"


def iter_interaction_batches(
    product_ids: list[str],
    item_bias: np.ndarray,
    item_factors: np.ndarray,
    user_ids_raw: list[str],
    user_bias: np.ndarray,
    user_factors: np.ndarray,
    seen_pairs: set[tuple[str, str]],
    batch_size: int = 5000,
):
    """Genera las interacciones en lotes para acotar el uso de RAM."""
    total_users = len(user_ids_raw)
    now = datetime.now(timezone.utc)
    remaining = TOTAL_INTERACTIONS
    cursor = 0

    while remaining > 0:
        size = min(batch_size, remaining)
        # Cada fila toma un usuario consecutivo (round-robin) para que todos
        # tengan al menos ~3 interacciones.
        user_idx = (np.arange(cursor, cursor + size) % total_users)
        cursor += size

        # 25 candidatos aleatorios por fila; se elige el de mayor afinidad.
        candidate_idx = RNG.integers(0, len(product_ids), size=(size, 25))
        uf = user_factors[user_idx]  # (size, LATENT_DIM)
        cf = item_factors[candidate_idx]  # (size, 25, LATENT_DIM)
        scores = np.einsum("sd,skd->sk", uf, cf) * 0.65
        scores += item_bias[candidate_idx]
        scores += user_bias[user_idx][:, None]
        scores += 3.6
        scores += RNG.normal(0, 0.05, size=(size, 25))
        best = np.argmax(scores, axis=1)
        chosen = candidate_idx[np.arange(size), best]
        latent = np.clip(scores[np.arange(size), best], 1.0, 5.0)

        draws = RNG.random(size)
        noise = RNG.normal(0, 0.16, size)
        rows: list[dict[str, Any]] = []

        for row_index in range(size):
            user_id = user_ids_raw[int(user_idx[row_index])]
            product_id = product_ids[int(chosen[row_index])]
            pair = (user_id, product_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            score = float(latent[row_index])
            event_type = pick_event(score, float(draws[row_index]))
            if event_type == "rating":
                rating = round(float(np.clip(score + noise[row_index] * 2.0, 1.0, 5.0)), 1)
            else:
                # Preferencia implícita inferida (señal de entrenamiento).
                rating = round(float(np.clip(score + noise[row_index], 1.0, 5.0)), 1)

            when = now - timedelta(
                days=int(RNG.integers(0, 120)),
                seconds=int(RNG.integers(0, 86_400)),
            )
            rows.append(
                {
                    "user_id": user_id,
                    "product_id": product_id,
                    "event_type": event_type,
                    "rating_value": rating,
                    "timestamp": when.replace(tzinfo=None),
                    "session_id": f"s_{hashlib.sha256(f'{user_id}{product_id}{when.timestamp()}'.encode()).hexdigest()[:16]}",
                    "idempotency_key": f"seed:{user_id}:{product_id}:{event_type}:{when.isoformat()}",
                }
            )
        if rows:
            yield rows
        remaining -= size

    # Garantiza historia rica para el usuario de la demo (u_1001 / p_1042).
    demo_pairs = [
        ("u_1001", "p_1042", "purchase", 5.0, 5),
        ("u_1001", "p_1001", "cart", 4.6, 9),
        ("u_1001", "p_1012", "click", 3.4, 3),
        ("u_1001", "p_1023", "rating", 4.8, 12),
        ("u_1001", "p_1034", "click", 3.8, 1),
        ("u_1001", "p_1045", "cart", 4.3, 7),
        ("u_1001", "p_1056", "click", 3.1, 14),
    ]
    demo_rows: list[dict[str, Any]] = []
    for user_id, product_id, event_type, rating, days_ago in demo_pairs:
        when = now - timedelta(days=days_ago, hours=3)
        demo_rows.append(
            {
                "user_id": user_id,
                "product_id": product_id,
                "event_type": event_type,
                "rating_value": rating,
                "timestamp": when.replace(tzinfo=None),
                "session_id": "s_demo_u1001",
                "idempotency_key": f"seed-demo:{user_id}:{product_id}:{event_type}",
            }
        )
    yield demo_rows


def table_count(conn, table: str) -> int:
    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def seed() -> dict[str, int]:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    for attempt in range(1, 61):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception as exc:  # pragma: no cover - espera de infraestructura
            log(f"mysql_no_disponible intento={attempt} error={type(exc).__name__}")
            time.sleep(2)
    else:
        raise RuntimeError("MySQL no disponible para sembrar datos")

    with engine.begin() as conn:
        existing_products = table_count(conn, "PRODUCTO")
        existing_users = table_count(conn, "USUARIO")
        existing_interactions = table_count(conn, "INTERACCION")

    if (
        existing_products >= TOTAL_PRODUCTS
        and existing_users >= TOTAL_USERS
        and existing_interactions >= 150_000
    ):
        log(
            f"seed_omitido products={existing_products} users={existing_users} "
            f"interactions={existing_interactions}"
        )
        return {
            "products": existing_products,
            "users": existing_users,
            "interactions": existing_interactions,
        }

    log(f"seed_inicio products={TOTAL_PRODUCTS} users={TOTAL_USERS} interactions={TOTAL_INTERACTIONS}")
    started = time.time()

    products = build_products()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT IGNORE INTO PRODUCTO
                  (product_id, name, category, price, stock, attributes)
                VALUES
                  (:product_id, :name, :category, :price, :stock, :attributes)
                """
            ),
            products,
        )
    log(f"productos_insertados={len(products)}")

    users = build_users()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT IGNORE INTO USUARIO
                  (user_id, email_hash, created_at, segment)
                VALUES
                  (:user_id, :email_hash, :created_at, :segment)
                """
            ),
            users,
        )
    log(f"usuarios_insertados={len(users)}")

    # Factores latentes: 8 dimensiones reales; el SVD de 50 las recupera.
    product_ids = [row["product_id"] for row in products]
    user_ids_raw = [row["user_id"] for row in users]
    item_factors = RNG.normal(0, 0.45, size=(len(product_ids), LATENT_DIM))
    item_bias = RNG.normal(0, 0.25, size=len(product_ids))
    user_factors = RNG.normal(0, 0.45, size=(len(user_ids_raw), LATENT_DIM))
    user_bias = RNG.normal(0, 0.22, size=len(user_ids_raw))

    interactions = iter_interaction_batches(
        product_ids=product_ids,
        item_bias=item_bias,
        item_factors=item_factors,
        user_ids_raw=user_ids_raw,
        user_bias=user_bias,
        user_factors=user_factors,
        seen_pairs=set(),
    )
    written = 0
    for batch in interactions:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT IGNORE INTO INTERACCION
                      (user_id, product_id, event_type, rating_value, `timestamp`,
                       session_id, idempotency_key)
                    VALUES
                      (:user_id, :product_id, :event_type, :rating_value, :timestamp,
                       :session_id, :idempotency_key)
                    """
                ),
                batch,
            )
        written += len(batch)
    log(f"interacciones_insertadas={written}")

    with engine.connect() as conn:
        counts = {
            "products": table_count(conn, "PRODUCTO"),
            "users": table_count(conn, "USUARIO"),
            "interactions": table_count(conn, "INTERACCION"),
        }
    log(f"seed_fin elapsed_s={time.time() - started:.1f} counts={counts}")
    return counts


if __name__ == "__main__":
    result = seed()
    print(json.dumps({"seed": result}, ensure_ascii=False))
    sys.exit(0 if result["interactions"] > 150_000 else 1)
