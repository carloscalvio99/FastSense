"""Logs JSON estructurados y anonimización de identificadores.

Regla de oro: ningún correo, nombre, teléfono o dirección se escribe en logs.
El user_id se registra hasheado con SHA-256 + salt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")

_EXTRA_FIELDS = (
    "request_id",
    "user_id",
    "latency_ms",
    "source",
    "status_code",
    "path",
    "method",
    "event_id",
    "version",
    "cache_hit",
)


def sha256_hex(value: str, salt: str = "") -> str:
    return hashlib.sha256(f"{salt}{value}".encode("utf-8")).hexdigest()


def anonymize_identifier(value: str | None) -> str | None:
    """Hash corto para trazabilidad sin exponer el identificador real."""
    if not value:
        return None
    return sha256_hex(str(value), settings.log_hash_salt)[:16]


def redact_path(path: str | None) -> str | None:
    """Sustituye segmentos identificadores por marcadores en las rutas de log."""
    if not path:
        return path
    if path.startswith("/recommendations/"):
        return "/recommendations/[user_id]"
    return path


def hash_pii(value: str | None) -> str | None:
    """Hash SHA-256 completo para correos u otros datos personales."""
    if not value:
        return None
    return sha256_hex(str(value).strip().lower(), settings.pii_salt)


def redact_pii(value: Any) -> Any:
    """Elimina correos y teléfonos de estructuras anidadas (defensa en profundidad)."""
    if isinstance(value, str):
        value = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        value = PHONE_RE.sub("[REDACTED_PHONE]", value)
        return value
    if isinstance(value, dict):
        return {key: redact_pii(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_pii(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_pii(record.getMessage()),
        }
        for field_name in _EXTRA_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = redact_pii(value)
        if record.exc_info:
            payload["exception"] = redact_pii(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level or settings.log_level)

    # Evita handlers duplicados cuando uvicorn importa la app en varios workers.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level or settings.log_level)


def get_logger(name: str = "fastsense.api") -> logging.Logger:
    return logging.getLogger(name)
