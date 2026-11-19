"""Ingesta asíncrona: SQS, idempotencia, SHA-256, Parquet/S3 y DLQ."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
from datetime import datetime, timezone

import pandas as pd
import pyarrow.parquet as pq

from anonymizer import contains_pii, hash_pii, sanitize_payload

from conftest import API_BASE_URL, BUCKET, SQS_DLQ_URL, SQS_QUEUE_URL, wait_until

PII_SALT = os.getenv("PII_SALT", "fastsense-pii-salt-2026")


def queue_depth(sqs_client) -> int:
    attributes = sqs_client.get_queue_attributes(
        QueueUrl=SQS_QUEUE_URL,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes.get("ApproximateNumberOfMessages", 0)) + int(
        attributes.get("ApproximateNumberOfMessagesNotVisible", 0)
    )


def send_message(sqs_client, body: dict) -> None:
    sqs_client.send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(body))


def event_body(event_id: str, user_id: str, product_id: str, **extra) -> dict:
    payload = {
        "event_id": event_id,
        "idempotency_key": event_id,
        "user_id": user_id,
        "product_id": product_id,
        "event_type": "click",
        "session_id": "s_pytest_sqs",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(extra)
    return payload


def fetch_interaction(mysql, idempotency_key: str):
    with mysql.cursor() as cursor:
        cursor.execute(
            "SELECT interaction_id, user_id, product_id, event_type, rating_value, idempotency_key "
            "FROM INTERACCION WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        return cursor.fetchall()


def find_parquet_event(s3_client, event_id: str, timeout: float = 120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        listing = s3_client.list_objects_v2(
            Bucket=BUCKET, Prefix="datalake/interactions/"
        )
        objects = sorted(
            listing.get("Contents", []),
            key=lambda item: item["LastModified"],
            reverse=True,
        )
        for obj in objects[:20]:
            body = s3_client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            table = pq.read_table(io.BytesIO(body))
            frame = table.to_pandas()
            if event_id in set(frame["event_id"].astype(str)):
                return obj["Key"], frame
        time.sleep(2)
    raise AssertionError(f"no se encontró el evento {event_id} en el Data Lake")


def test_api_event_reaches_mysql_through_sqs(client, mysql):
    response = client.post(
        f"{API_BASE_URL}/track-interaction",
        json={
            "user_id": "u_1001",
            "product_id": "p_1042",
            "event_type": "cart",
            "session_id": "s_pytest_pipeline",
        },
        timeout=5,
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]

    wait_until(
        lambda: len(fetch_interaction(mysql, event_id)) == 1,
        timeout=90,
        message="el worker no persistió el evento en MySQL",
    )
    row = fetch_interaction(mysql, event_id)[0]
    assert row["event_type"] == "cart"
    assert row["product_id"] == "p_1042"
    assert row["idempotency_key"] == event_id


def test_worker_is_idempotent_with_duplicate_messages(mysql, sqs_client):
    event_id = f"pytest-idem-{uuid.uuid4().hex}"
    user_id = f"u_pytest_idem_{uuid.uuid4().hex[:10]}"
    body = event_body(event_id, user_id, "p_1042")

    send_message(sqs_client, body)
    send_message(sqs_client, body)

    wait_until(
        lambda: len(fetch_interaction(mysql, event_id)) >= 1,
        timeout=90,
        message="el worker no procesó el mensaje duplicado",
    )
    wait_until(
        lambda: queue_depth(sqs_client) == 0,
        timeout=90,
        message="la cola SQS no quedó vacía",
    )
    rows = fetch_interaction(mysql, event_id)
    assert len(rows) == 1, "INSERT IGNORE debe garantizar exactly-once lógico"


def test_sha256_anonymization_end_to_end(client, mysql, s3_client):
    suffix = uuid.uuid4().hex[:10]
    email = f"persona.{suffix}@example.com"
    user_id = f"u_pytest_pii_{suffix}"

    response = client.post(
        f"{API_BASE_URL}/track-interaction",
        json={
            "user_id": user_id,
            "product_id": "p_1050",
            "event_type": "rating",
            "rating": 4.7,
            "email": email,
            "session_id": "s_pytest_pii",
        },
        timeout=5,
    )
    assert response.status_code == 202
    api_event_id = response.json()["event_id"]

    wait_until(
        lambda: len(fetch_interaction(mysql, api_event_id)) == 1,
        timeout=90,
        message="el evento con PII no llegó a MySQL",
    )
    expected = hashlib.sha256(f"{PII_SALT}{email.strip().lower()}".encode()).hexdigest()
    with mysql.cursor() as cursor:
        cursor.execute("SELECT email_hash FROM USUARIO WHERE user_id = %s", (user_id,))
        stored = cursor.fetchone()["email_hash"]
    assert stored == expected
    assert "@" not in stored and len(stored) == 64

    key, frame = find_parquet_event(s3_client, api_event_id)
    assert key.startswith("datalake/interactions/date=")
    assert key.endswith(".parquet")
    assert "email" not in frame.columns
    assert "email_hash" not in frame.columns
    for column in frame.columns:
        if frame[column].dtype == object:
            assert not frame[column].astype(str).str.contains("@").any()
    assert set(frame["event_id"].astype(str)) >= {api_event_id}


def test_anonymizer_unit_sha256_is_deterministic():
    email = "Estudiante@Universidad.Cl"
    first = sanitize_payload({"email": email, "note": f"contacto: {email}"}, PII_SALT)
    second = sanitize_payload({"email": email}, PII_SALT)

    assert first["email_hash"] == second["email_hash"]
    assert len(first["email_hash"]) == 64
    assert first["email_hash"] == hash_pii(email, PII_SALT)
    assert "sha256:" in first["note"]
    assert contains_pii(first) is False
    assert email not in json.dumps(first)
    assert contains_pii({"email": email}) is True


def test_sqs_dlq_redrive_policy(sqs_client):
    attributes = sqs_client.get_queue_attributes(
        QueueUrl=SQS_QUEUE_URL, AttributeNames=["RedrivePolicy"]
    )["Attributes"]
    policy = json.loads(attributes["RedrivePolicy"])
    assert str(policy["maxReceiveCount"]) == "3"
    assert "dlq" in policy["deadLetterTargetArn"].lower()

    dlq_attributes = sqs_client.get_queue_attributes(
        QueueUrl=SQS_DLQ_URL, AttributeNames=["QueueArn"]
    )["Attributes"]
    assert dlq_attributes["QueueArn"].endswith("fastsense-interactions-dlq")


def test_s3_bucket_has_versioning(s3_client):
    versioning = s3_client.get_bucket_versioning(Bucket=BUCKET)
    assert versioning.get("Status") == "Enabled"


def test_top_global_is_materialized(redis_client):
    payload = redis_client.get("top:global")
    assert payload, "top:global debe existir para el fallback"
    items = json.loads(payload)
    assert len(items) >= 10
    assert all(
        {"product_id", "name", "category", "price", "score", "rank"} <= set(item)
        for item in items
    )
