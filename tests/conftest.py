"""Fixtures compartidas por la suite de aceptación de FastSense."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import boto3
import pymysql
import pytest
import redis
import requests
from botocore.config import Config
from pymysql.cursors import DictCursor

API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8080").rstrip("/")
NGINX_BASE_URL = os.getenv("NGINX_BASE_URL", "http://nginx:8080").rstrip("/")
API_KEY = os.getenv("API_KEY", "fs_demo_key_2026")
JWT_TOKEN = os.getenv("JWT_TOKEN", "")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "fastsense_internal_2026")
READY_TIMEOUT = int(os.getenv("READY_TIMEOUT_SECONDS", "300"))

MYSQL = {
    "host": os.getenv("MYSQL_HOST", "mysql"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "fastsense"),
    "password": os.getenv("MYSQL_PASSWORD", "fastsense"),
    "database": os.getenv("MYSQL_DATABASE", "fastsense"),
    "charset": "utf8mb4",
}

AWS_KWARGS: dict[str, Any] = {
    "region_name": os.getenv("AWS_REGION", "us-east-1"),
    "config": Config(retries={"max_attempts": 3}),
}
if os.getenv("AWS_ENDPOINT_URL"):
    AWS_KWARGS["endpoint_url"] = os.getenv("AWS_ENDPOINT_URL")
if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
    AWS_KWARGS["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_KWARGS["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")

BUCKET = os.getenv("S3_BUCKET", "fastsense-datalake-models")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
SQS_DLQ_URL = os.getenv("SQS_DLQ_URL", "")


def auth_headers() -> dict[str, str]:
    return {
        "X-API-Key": API_KEY,
        "Authorization": f"Bearer {JWT_TOKEN}",
        "X-Demo-Bypass": "fastsense-demo-bypass",
    }


def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 60.0,
    interval: float = 1.0,
    message: str = "condición no alcanzada",
) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # pragma: no cover - diagnóstico
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (último error: {last_error})")


@pytest.fixture(scope="session", autouse=True)
def wait_ready() -> None:
    """Espera a que AMBOS workers de uvicorn tengan el modelo cargado.

    /ready responde 503 si Redis, MySQL o model_loaded fallan. Se exigen
    varias respuestas 200 consecutivas para cubrir el balanceo entre pods.
    """
    deadline = time.time() + READY_TIMEOUT
    consecutive = 0
    last = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{API_BASE_URL}/ready", timeout=5)
            last = response.status_code
            if response.status_code == 200:
                consecutive += 1
                if consecutive >= 8:
                    return
            else:
                consecutive = 0
        except requests.RequestException:
            consecutive = 0
        time.sleep(2)
    raise AssertionError(f"La API no quedó ready en {READY_TIMEOUT}s (último status={last})")


@pytest.fixture(scope="session")
def client() -> requests.Session:
    session = requests.Session()
    session.headers.update(auth_headers())
    return session


@pytest.fixture(scope="session")
def mysql() -> Any:
    deadline = time.time() + 120
    while True:
        try:
            connection = pymysql.connect(**MYSQL, cursorclass=DictCursor, autocommit=True)
            break
        except pymysql.MySQLError:
            if time.time() > deadline:
                raise
            time.sleep(2)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True
    )


@pytest.fixture(scope="session")
def s3_client():
    return boto3.client("s3", **AWS_KWARGS)


@pytest.fixture(scope="session")
def sqs_client():
    return boto3.client("sqs", **AWS_KWARGS)
