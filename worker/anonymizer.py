"""Anonimización de PII (stdlib puro, testeable sin dependencias pesadas)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")


def _looks_like_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 9:
        return False
    # Evita falsos positivos con fechas ISO y timestamps.
    return any(marker in value for marker in ("+", " ", "(", ")"))


def hash_pii(value: str, salt: str = "") -> str:
    return hashlib.sha256(f"{salt}{value.strip().lower()}".encode("utf-8")).hexdigest()


def contains_pii(value: Any) -> bool:
    if isinstance(value, str):
        return bool(EMAIL_RE.search(value) or (PHONE_RE.search(value) and _looks_like_phone(value)))
    if isinstance(value, dict):
        return any(contains_pii(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_pii(item) for item in value)
    return False


def sanitize_payload(value: Any, salt: str = "") -> Any:
    """Reemplaza correos/teléfonos por su hash SHA-256.

    - La clave ``email`` (o similar) se convierte en ``email_hash``.
    - Cualquier correo embebido en otro string se sustituye por ``sha256:<hex>``.
    - No queda ningún valor en claro que permita identificar a la persona.
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"email", "correo", "e-mail", "mail"} and isinstance(item, str):
                cleaned["email_hash"] = hash_pii(item, salt)
                continue
            if isinstance(item, str) and EMAIL_RE.search(item):
                cleaned[key] = EMAIL_RE.sub(
                    lambda match: f"sha256:{hash_pii(match.group(0), salt)}", item
                )
                continue
            cleaned[key] = sanitize_payload(item, salt)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item, salt) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_payload(item, salt) for item in value)
    return value
