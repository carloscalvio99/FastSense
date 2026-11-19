"""Entrenamiento SVD + Quality Gate + promoción MLOps.

Flujo:
  1. Lee interacciones con preferencia (rating_value) desde MySQL.
  2. Entrena SVD(n_factors=50, n_epochs=20, random_state=42) con holdout 20%.
  3. Evalúa RMSE. Si RMSE > 0.85 el modelo NO se publica (gate).
  4. Serializa con joblib.dump(compress=('zlib',3)) y sube a S3.
  5. Publica manifest, registra MODELO_VERSION y actualiza top:global.
  6. Dispara el hot reload de la API (sin reiniciar pods).
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3
import joblib
import numpy as np
import pandas as pd
import redis
import requests
from botocore.config import Config
from surprise import SVD, Dataset, Reader, accuracy
from surprise.model_selection import train_test_split
from sqlalchemy import create_engine, text

from quality_gate import evaluate_quality_gate

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://fastsense:fastsense@mysql:3306/fastsense?charset=utf8mb4",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL") or None
BUCKET = os.getenv("S3_BUCKET", "fastsense-datalake-models")
MODEL_KEY = os.getenv("S3_MODEL_KEY", "models/model.pkl")
MANIFEST_KEY = os.getenv("S3_MANIFEST_KEY", "models/manifest.json")
N_FACTORS = int(os.getenv("N_FACTORS", "50"))
N_EPOCHS = int(os.getenv("N_EPOCHS", "20"))
TEST_SIZE = float(os.getenv("TEST_SIZE", "0.2"))
RMSE_GATE = float(os.getenv("RMSE_GATE", "0.85"))
SEED = int(os.getenv("SEED", "42"))
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "fastsense_internal_2026")
API_RELOAD_URL = os.getenv("API_RELOAD_URL", "http://api:8080/internal/reload-model")
ARTIFACT_PATH = os.getenv("ARTIFACT_PATH", "/tmp/artifacts/model.pkl")
RESULT_PATH = os.getenv("RESULT_PATH", "/tmp/training_result.json")
PII_SALT = os.getenv("PII_SALT", "fastsense-pii-salt-2026")
MIN_RATINGS = int(os.getenv("MIN_RATINGS", "1000"))
# Umbral de "relevante" para Precision@10 (la señal media de la semilla es 4.44).
RELEVANT_RATING = float(os.getenv("RELEVANT_RATING", "4.5"))


def log(message: str, **extra: object) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "service": "ml-trainer",
        "message": message,
    }
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def aws_kwargs() -> dict:
    kwargs: dict = {"region_name": AWS_REGION, "config": Config(retries={"max_attempts": 5})}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    access = os.getenv("AWS_ACCESS_KEY_ID")
    secret = os.getenv("AWS_SECRET_ACCESS_KEY")
    if access and secret:
        kwargs["aws_access_key_id"] = access
        kwargs["aws_secret_access_key"] = secret
    return kwargs


def wait_for_data(timeout_s: int = 900) -> pd.DataFrame:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                total = int(conn.execute(text("SELECT COUNT(*) FROM INTERACCION")).scalar() or 0)
                rated = int(
                    conn.execute(
                        text("SELECT COUNT(*) FROM INTERACCION WHERE rating_value IS NOT NULL")
                    ).scalar()
                    or 0
                )
            if total > 150_000 and rated >= MIN_RATINGS:
                frame = pd.read_sql(
                    text(
                        "SELECT user_id, product_id, rating_value "
                        "FROM INTERACCION WHERE rating_value IS NOT NULL"
                    ),
                    engine,
                )
                # DECIMAL(3,2) llega como decimal.Decimal; Surprise requiere floats.
                frame["rating_value"] = frame["rating_value"].astype(float)
                frame["user_id"] = frame["user_id"].astype(str)
                frame["product_id"] = frame["product_id"].astype(str)
                log("dataset_loaded", interactions=total, ratings=int(len(frame)))
                return frame
            log("waiting_for_seed", interactions=total, ratings=rated)
        except Exception as exc:
            log("mysql_wait", error=type(exc).__name__)
        time.sleep(5)
    raise RuntimeError("No hay datos suficientes para entrenar")


def precision_at_10(model: SVD, testset, sample_size: int = 400) -> float:
    """Precision@10 sobre el conjunto candidato dejado fuera por usuario."""
    by_user: dict[str, list[tuple[str, float]]] = {}
    for user_id, product_id, rating in testset:
        by_user.setdefault(user_id, []).append((product_id, float(rating)))

    users = [user for user, values in by_user.items() if len(values) >= 2]
    if not users:
        return 0.0
    rng = np.random.default_rng(SEED)
    if len(users) > sample_size:
        users = list(rng.choice(users, size=sample_size, replace=False))

    precisions: list[float] = []
    # knows_user()/knows_item() esperan IDs internos, pero el testset trae IDs
    # crudos: se consultan los mapas raw->inner del trainset directamente.
    raw_users = model.trainset._raw2inner_id_users  # noqa: SLF001
    raw_items = model.trainset._raw2inner_id_items  # noqa: SLF001
    for user_id in users:
        # Un usuario puede quedar completamente fuera del trainset (cold start
        # del holdout); Surprise lanza ValueError al predecir.
        if user_id not in raw_users:
            continue
        candidates = [
            (item, rating)
            for item, rating in by_user[user_id]
            if item in raw_items
        ]
        if not candidates:
            continue
        relevant = {item for item, rating in candidates if rating >= RELEVANT_RATING}
        if not relevant:
            continue
        ranked = sorted(
            candidates,
            key=lambda pair: model.predict(user_id, pair[0]).est,
            reverse=True,
        )
        top = ranked[:10]
        hits = sum(1 for item, _ in top if item in relevant)
        precisions.append(hits / min(10, len(candidates)))
    return float(np.mean(precisions)) if precisions else 0.0


def build_top_global(engine, limit: int = 20) -> list[dict]:
    query = text(
        """
        SELECT p.product_id, p.name, p.category, p.price,
               SUM(CASE i.event_type
                       WHEN 'purchase' THEN 5.0
                       WHEN 'cart'     THEN 3.0
                       WHEN 'rating'   THEN GREATEST(COALESCE(i.rating_value, 3), 1)
                       ELSE 1.0 END) AS popularity
        FROM INTERACCION i
        JOIN PRODUCTO p ON p.product_id = i.product_id
        GROUP BY p.product_id, p.name, p.category, p.price
        ORDER BY popularity DESC, p.product_id ASC
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"lim": limit}).mappings().all()
    max_pop = max((float(row["popularity"]) for row in rows), default=1.0) or 1.0
    return [
        {
            "product_id": row["product_id"],
            "name": row["name"],
            "category": row["category"],
            "price": float(row["price"]),
            "score": round(float(row["popularity"]) / max_pop, 4),
            "rank": index,
        }
        for index, row in enumerate(rows, start=1)
    ]


def register_model(engine, version: str, rmse: float, precision: float, s3_uri: str, status: str) -> None:
    with engine.begin() as conn:
        if status == "active":
            conn.execute(text("UPDATE MODELO_VERSION SET status='archived' WHERE status='active'"))
        conn.execute(
            text(
                """
                INSERT INTO MODELO_VERSION
                  (version, rmse, precision_at_10, s3_uri, trained_at, status)
                VALUES (:version, :rmse, :precision, :s3_uri, :trained_at, :status)
                ON DUPLICATE KEY UPDATE
                  rmse=VALUES(rmse), precision_at_10=VALUES(precision_at_10),
                  s3_uri=VALUES(s3_uri), trained_at=VALUES(trained_at),
                  status=VALUES(status)
                """
            ),
            {
                "version": version,
                "rmse": round(float(rmse), 4),
                "precision": round(float(precision), 4),
                "s3_uri": s3_uri,
                "trained_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "status": status,
            },
        )
    log("model_registered", version=version, status=status)


def trigger_reload() -> bool:
    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    for attempt in range(1, 16):
        try:
            response = requests.post(API_RELOAD_URL, headers=headers, timeout=10)
            if response.status_code == 200:
                log("hot_reload_ok", attempt=attempt, body=response.json())
                return True
            log("hot_reload_pending", attempt=attempt, status=response.status_code)
        except requests.RequestException as exc:
            log("hot_reload_retry", attempt=attempt, error=type(exc).__name__)
        time.sleep(2)
    return False


def main() -> int:
    started = time.time()
    frame = wait_for_data()

    reader = Reader(rating_scale=(1.0, 5.0))
    # Muestra acotada por memoria: 180k filas es manejable, pero se respeta un
    # techo configurable para entornos con poca RAM.
    max_ratings = int(os.getenv("MAX_TRAIN_RATINGS", "400000"))
    if len(frame) > max_ratings:
        frame = frame.sample(n=max_ratings, random_state=SEED)
        log("dataset_sampled", ratings=int(len(frame)))

    dataset = Dataset.load_from_df(
        frame.rename(columns={"rating_value": "rating"})[["user_id", "product_id", "rating"]],
        reader,
    )
    trainset, testset = train_test_split(dataset, test_size=TEST_SIZE, random_state=SEED)

    log("training_start", n_factors=N_FACTORS, n_epochs=N_EPOCHS, ratings=len(frame))
    model = SVD(n_factors=N_FACTORS, n_epochs=N_EPOCHS, random_state=SEED, verbose=False)
    model.fit(trainset)
    predictions = model.test(testset)
    rmse = float(accuracy.rmse(predictions, verbose=False))
    precision = precision_at_10(model, testset)
    gate = evaluate_quality_gate(rmse, RMSE_GATE, precision)
    log("evaluation_complete", **gate)

    version = f"svd-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

    if not gate["passed"]:
        register_model(
            engine, version, rmse, precision, "s3://rejected/not-published", "rejected"
        )
        result = {
            "status": "rejected",
            "version": version,
            "rmse": rmse,
            "precision_at_10": precision,
            "gate": gate,
            "elapsed_s": round(time.time() - started, 1),
        }
        with open(RESULT_PATH, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
        log("quality_gate_rejected", version=version, rmse=rmse)
        return 1

    # Reentrenar con el 100% de los datos para servir a todos los usuarios.
    log("refit_full_dataset", users=int(frame["user_id"].nunique()))
    # Liberar el modelo de evaluación antes del refit para acotar el pico de RAM
    # en hosts con poca memoria (el contenedor Docker tiene ~1.8 GB).
    import gc

    del predictions, testset, trainset, model
    gc.collect()

    full_trainset = dataset.build_full_trainset()
    production_model = SVD(n_factors=N_FACTORS, n_epochs=N_EPOCHS, random_state=SEED, verbose=False)
    production_model.fit(full_trainset)

    os.makedirs(os.path.dirname(ARTIFACT_PATH), exist_ok=True)
    joblib.dump(production_model, ARTIFACT_PATH, compress=("zlib", 3))
    artifact_bytes = os.path.getsize(ARTIFACT_PATH)

    s3_uri = f"s3://{BUCKET}/{MODEL_KEY}"
    trained_at = datetime.now(timezone.utc)
    manifest = {
        "version": version,
        "model_key": MODEL_KEY,
        "rmse": round(rmse, 4),
        "precision_at_10": round(precision, 4),
        "trained_at": trained_at.isoformat(),
        "s3_uri": s3_uri,
        "n_users": int(full_trainset.n_users),
        "n_items": int(full_trainset.n_items),
        "n_ratings": int(full_trainset.n_ratings),
        "n_factors": N_FACTORS,
        "n_epochs": N_EPOCHS,
        "seed": SEED,
        "artifact_bytes": artifact_bytes,
        "artifact_compression": "zlib-3",
        "gate": {"passed": True, "rmse_max": RMSE_GATE},
    }

    s3 = boto3.client("s3", **aws_kwargs())
    s3.upload_file(ARTIFACT_PATH, BUCKET, MODEL_KEY)
    s3.put_object(
        Bucket=BUCKET,
        Key=MANIFEST_KEY,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    log("artifact_published", s3_uri=s3_uri, bytes=artifact_bytes)

    register_model(engine, version, rmse, precision, s3_uri, "active")

    top_global = build_top_global(engine)
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        redis_client.set("top:global", json.dumps(top_global, ensure_ascii=False))
        log("top_global_updated", items=len(top_global))
    except redis.RedisError as exc:
        log("top_global_error", error=type(exc).__name__)

    reloaded = trigger_reload()
    result = {
        "status": "promoted",
        "version": version,
        "rmse": round(rmse, 4),
        "precision_at_10": round(precision, 4),
        "gate": gate,
        "s3_uri": s3_uri,
        "artifact_bytes": artifact_bytes,
        "hot_reload": reloaded,
        "elapsed_s": round(time.time() - started, 1),
    }
    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    log("pipeline_complete", **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
