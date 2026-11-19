"""Configuración centralizada por variables de entorno.

El mismo binario corre en Docker Compose y en AWS: la única diferencia son
las variables (endpoint de LocalStack vs. endpoints reales de AWS).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "y", "on"}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Aplicación
    app_name: str = "FastSense Inference API"
    app_version: str = _env("APP_VERSION", "3.0.0")
    environment: str = _env("ENVIRONMENT", "local")
    log_level: str = _env("LOG_LEVEL", "INFO").upper()
    docs_enabled: bool = _env_bool("DOCS_ENABLED", True)

    # Seguridad
    api_key: str = _env("API_KEY", "fs_demo_key_2026")
    jwt_secret: str = _env("JWT_SECRET", "fastsense_local_jwt_secret_2026_change_me")
    internal_token: str = _env("INTERNAL_TOKEN", "fastsense_internal_2026")
    pii_salt: str = _env("PII_SALT", "fastsense-pii-salt-2026")
    log_hash_salt: str = _env("LOG_HASH_SALT", "fastsense-log-salt-2026")

    # Modelo / inferencia
    model_dir: str = _env("MODEL_DIR", "/tmp/fastsense-models")
    svd_timeout_ms: int = _env_int("SVD_TIMEOUT_MS", 400)
    circuit_breaker_fail_max: int = _env_int("CIRCUIT_BREAKER_FAIL_MAX", 3)
    circuit_breaker_reset_s: int = _env_int("CIRCUIT_BREAKER_RESET_S", 30)
    model_poll_seconds: int = _env_int("MODEL_POLL_SECONDS", 10)
    default_limit: int = 10
    max_limit: int = 20

    # Caché
    redis_url: str = _env("REDIS_URL", "redis://redis:6379/0")
    cache_ttl_seconds: int = _env_int("CACHE_TTL_SECONDS", 3600)

    # Base de datos
    database_url: str = _env(
        "DATABASE_URL",
        "mysql+pymysql://fastsense:fastsense@mysql:3306/fastsense?charset=utf8mb4",
    )
    db_pool_size: int = _env_int("DB_POOL_SIZE", 10)

    # AWS / LocalStack
    aws_region: str = _env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1"))
    aws_endpoint_url: Optional[str] = field(
        default_factory=lambda: _env("AWS_ENDPOINT_URL") or None
    )
    aws_access_key_id: Optional[str] = field(
        default_factory=lambda: _env("AWS_ACCESS_KEY_ID") or None
    )
    aws_secret_access_key: Optional[str] = field(
        default_factory=lambda: _env("AWS_SECRET_ACCESS_KEY") or None
    )
    s3_bucket: str = _env("S3_BUCKET", "fastsense-datalake-models")
    s3_model_key: str = _env("S3_MODEL_KEY", "models/model.pkl")
    s3_manifest_key: str = _env("S3_MANIFEST_KEY", "models/manifest.json")
    sqs_queue_url: str = _env("SQS_QUEUE_URL")
    sqs_dlq_url: str = _env("SQS_DLQ_URL")
    sqs_queue_name: str = _env("SQS_QUEUE_NAME", "fastsense-interactions-queue")

    # CORS
    cors_origins: list[str] = field(
        default_factory=lambda: _split_csv(
            _env("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
        )
    )

    @property
    def svd_timeout_seconds(self) -> float:
        return max(0.05, self.svd_timeout_ms / 1000.0)


settings = Settings()
