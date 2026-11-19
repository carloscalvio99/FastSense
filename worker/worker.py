"""FastSense · Worker de Ingesta Asíncrona.

SQS (micro-lotes, long polling) -> anonimización SHA-256 -> INSERT IGNORE en
MySQL (idempotencia por event_id) -> Parquet en S3 (Data Lake particionado).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import create_engine, text

from anonymizer import contains_pii, hash_pii, sanitize_payload

# =====================================================================
# Configuración
# =====================================================================
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://fastsense:fastsense@mysql:3306/fastsense?charset=utf8mb4",
)
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL") or None
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID") or None
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY") or None
QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
QUEUE_NAME = os.getenv("SQS_QUEUE_NAME", "fastsense-interactions-queue")
DLQ_URL = os.getenv("SQS_DLQ_URL", "")
BUCKET = os.getenv("S3_BUCKET", "fastsense-datalake-models")
LAKE_PREFIX = os.getenv("S3_LAKE_PREFIX", "datalake/interactions")
PII_SALT = os.getenv("PII_SALT", "fastsense-pii-salt-2026")
WAIT_TIME = int(os.getenv("SQS_WAIT_TIME_SECONDS", "5"))
MAX_MESSAGES = int(os.getenv("SQS_MAX_MESSAGES", "10"))
POLL_INTERVAL = float(os.getenv("SQS_POLL_INTERVAL_SECONDS", "1"))
HEARTBEAT = os.getenv("WORKER_HEARTBEAT", "/tmp/fastsense-worker.heartbeat")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

ALLOWED_EVENTS = {"click", "cart", "purchase", "rating"}
RESOLVED_QUEUE_URL = QUEUE_URL
_running = True


# =====================================================================
# Logs JSON (sin PII)
# =====================================================================
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": "fastsense-worker",
            "message": record.getMessage(),
        }
        for key in ("batch_id", "messages", "inserted", "duplicates", "errors", "parquet_key", "event_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=LOG_LEVEL, handlers=[handler])
logger = logging.getLogger("fastsense.worker")


def _aws_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"region_name": AWS_REGION, "config": Config(retries={"max_attempts": 3})}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    return kwargs


sqs = boto3.client("sqs", **_aws_kwargs())
s3 = boto3.client("s3", **_aws_kwargs())
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    connect_args={"connect_timeout": 3},
    future=True,
)


# =====================================================================
# Helpers
# =====================================================================
def heartbeat() -> None:
    try:
        with open(HEARTBEAT, "w", encoding="utf-8") as handle:
            handle.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def resolve_queue_url() -> str:
    if QUEUE_URL:
        return QUEUE_URL
    return sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]


def wait_for_dependencies(timeout_s: int = 120) -> str:
    deadline = time.time() + timeout_s
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            url = resolve_queue_url()
            sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
            logger.info("dependencies_ready queue=%s", url)
            return url
        except Exception as exc:  # pragma: no cover - depende del entorno
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"dependencias no disponibles: {last_error}")


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def build_row(message: dict[str, Any]) -> dict[str, Any]:
    clean = sanitize_payload(message, PII_SALT)
    event_type = str(clean.get("event_type", "")).lower()
    if event_type not in ALLOWED_EVENTS:
        raise ValueError(f"event_type inválido: {event_type!r}")

    user_id = str(clean.get("user_id", "")).strip()
    product_id = str(clean.get("product_id", "")).strip()
    if not user_id or not product_id:
        raise ValueError("user_id/product_id requeridos")

    event_id = str(clean.get("event_id") or clean.get("idempotency_key") or "").strip()
    if not event_id:
        canonical = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)
        event_id = f"idem_{hashlib.sha256(canonical.encode()).hexdigest()[:40]}"

    rating = clean.get("rating")
    if rating is not None:
        try:
            rating = max(1.0, min(5.0, float(rating)))
        except (TypeError, ValueError):
            rating = None

    email_hash = clean.get("email_hash")
    if not email_hash:
        email_hash = hash_pii(f"{user_id}@pseudonymous.local", PII_SALT)

    return {
        "event_id": event_id,
        "idempotency_key": event_id,
        "user_id": user_id,
        "product_id": product_id,
        "event_type": event_type,
        "rating": rating,
        "timestamp": parse_timestamp(clean.get("timestamp")),
        "session_id": str(clean.get("session_id") or "")[:64] or None,
        "request_id": str(clean.get("request_id") or "")[:80] or None,
        "email_hash": email_hash,
        "ingested_at": datetime.now(timezone.utc),
    }


def persist_user(conn, row: dict[str, Any]) -> None:
    conn.execute(
        text(
            """
            INSERT INTO USUARIO (user_id, email_hash, created_at, segment)
            VALUES (:user_id, :email_hash, NOW(), 'unknown')
            ON DUPLICATE KEY UPDATE
              email_hash = COALESCE(NULLIF(VALUES(email_hash), ''), email_hash)
            """
        ),
        {"user_id": row["user_id"], "email_hash": row["email_hash"]},
    )


def persist_interaction(conn, row: dict[str, Any]) -> bool:
    result = conn.execute(
        text(
            """
            INSERT IGNORE INTO INTERACCION
              (user_id, product_id, event_type, rating_value, `timestamp`,
               session_id, idempotency_key)
            VALUES
              (:user_id, :product_id, :event_type, :rating, :ts,
               :session_id, :idempotency_key)
            """
        ),
        {
            "user_id": row["user_id"],
            "product_id": row["product_id"],
            "event_type": row["event_type"],
            "rating": row["rating"],
            "ts": row["timestamp"],
            "session_id": row["session_id"],
            "idempotency_key": row["idempotency_key"],
        },
    )
    return bool(result.rowcount)


def write_parquet(rows: list[dict[str, Any]], batch_id: str) -> str:
    """Escribe el lote en el Data Lake (sin PII) y devuelve la clave S3."""
    frame = pd.DataFrame(
        [
            {
                "event_id": row["event_id"],
                "user_id": row["user_id"],
                "product_id": row["product_id"],
                "event_type": row["event_type"],
                "rating": row["rating"],
                "timestamp": row["timestamp"],
                "session_id": row["session_id"],
                "request_id": row["request_id"],
                "ingested_at": row["ingested_at"],
            }
            for row in rows
        ]
    )
    table = pa.Table.from_pandas(frame, preserve_index=False)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    buffer.seek(0)

    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{LAKE_PREFIX}/date={date_part}/batch_{batch_id}.parquet"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="application/vnd.apache.parquet",
    )
    return key


# =====================================================================
# Procesamiento de lote
# =====================================================================
def process_messages(messages: list[dict[str, Any]]) -> None:
    batch_id = uuid.uuid4().hex[:12]
    rows: list[dict[str, Any]] = []
    receipts_to_delete: list[str] = []
    errors = 0
    duplicates = 0

    for message in messages:
        receipt = message.get("ReceiptHandle", "")
        try:
            body = json.loads(message.get("Body", "{}"))
            if not isinstance(body, dict):
                raise ValueError("body no es un objeto JSON")
            row = build_row(body)
            with engine.begin() as conn:
                persist_user(conn, row)
                inserted = persist_interaction(conn, row)
            rows.append(row)
            receipts_to_delete.append(receipt)
            if not inserted:
                duplicates += 1
            if contains_pii(body):  # el body original no se persiste ni loguea
                logger.warning("pii_detected_and_hashed event_id=%s", row["event_id"])
        except Exception as exc:
            errors += 1
            logger.error(
                "message_failed error=%s message_id=%s",
                type(exc).__name__,
                message.get("MessageId", "?"),
            )

    parquet_key = None
    if rows:
        try:
            parquet_key = write_parquet(rows, batch_id)
        except Exception as exc:
            # No se borran mensajes: SQS los reentrega (at-least-once).
            logger.error("parquet_write_failed error=%s", type(exc).__name__)
            return

    for receipt in receipts_to_delete:
        try:
            sqs.delete_message(QueueUrl=RESOLVED_QUEUE_URL, ReceiptHandle=receipt)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("delete_message_failed error=%s", type(exc).__name__)

    logger.info(
        "batch_processed messages=%d inserted_or_duplicate=%d duplicates=%d errors=%d parquet_key=%s",
        len(messages),
        len(rows),
        duplicates,
        errors,
        parquet_key,
        extra={
            "batch_id": batch_id,
            "messages": len(messages),
            "inserted": len(rows) - duplicates,
            "duplicates": duplicates,
            "errors": errors,
            "parquet_key": parquet_key,
        },
    )


# =====================================================================
# Loop principal
# =====================================================================
def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _running
    _running = False
    logger.info("shutdown_signal signal=%s", signum)


def main() -> None:
    global RESOLVED_QUEUE_URL
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    RESOLVED_QUEUE_URL = wait_for_dependencies()
    logger.info("worker_started queue=%s max_messages=%d wait=%ds", RESOLVED_QUEUE_URL, MAX_MESSAGES, WAIT_TIME)
    heartbeat()

    while _running:
        try:
            response = sqs.receive_message(
                QueueUrl=RESOLVED_QUEUE_URL,
                MaxNumberOfMessages=MAX_MESSAGES,
                WaitTimeSeconds=WAIT_TIME,
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            )
            messages = response.get("Messages", [])
            heartbeat()
            if messages:
                process_messages(messages)
            elif POLL_INTERVAL > 0:
                time.sleep(POLL_INTERVAL)
        except (BotoCoreError, ClientError) as exc:
            logger.error("sqs_poll_error error=%s", type(exc).__name__)
            time.sleep(2)
        except Exception as exc:  # pragma: no cover
            logger.error("worker_loop_error error=%s", type(exc).__name__)
            time.sleep(2)

    heartbeat()
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
