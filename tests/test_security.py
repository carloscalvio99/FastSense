"""Seguridad: API Key + JWT HS256, endpoints públicos e internos."""

from __future__ import annotations

import os
import time

import jwt
import requests

from conftest import API_BASE_URL, API_KEY, INTERNAL_TOKEN, JWT_TOKEN


def test_missing_credentials_returns_401():
    response = requests.get(f"{API_BASE_URL}/recommendations/u_1001", timeout=5)
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_invalid_api_key_returns_401():
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001",
        headers={
            "X-API-Key": "clave-invalida",
            "Authorization": f"Bearer {JWT_TOKEN}",
        },
        timeout=5,
    )
    assert response.status_code == 401


def test_missing_jwt_returns_401():
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001",
        headers={"X-API-Key": API_KEY},
        timeout=5,
    )
    assert response.status_code == 401


def test_invalid_signature_returns_401():
    forged = jwt.encode({"sub": "atacante", "exp": int(time.time()) + 3600}, "otro-secreto", algorithm="HS256")
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001",
        headers={"X-API-Key": API_KEY, "Authorization": f"Bearer {forged}"},
        timeout=5,
    )
    assert response.status_code == 401


def test_expired_jwt_returns_401():
    secret = os.getenv("JWT_SECRET", "fastsense_local_jwt_secret_2026_change_me")
    expired = jwt.encode(
        {"sub": "shopfast", "exp": int(time.time()) - 60}, secret, algorithm="HS256"
    )
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001",
        headers={"X-API-Key": API_KEY, "Authorization": f"Bearer {expired}"},
        timeout=5,
    )
    assert response.status_code == 401


def test_public_endpoints_do_not_require_auth():
    for path in ("/health", "/ready", "/docs", "/openapi.json"):
        response = requests.get(f"{API_BASE_URL}{path}", timeout=5)
        assert response.status_code == 200, f"{path} debería ser público"


def test_internal_endpoint_requires_internal_token(client):
    forbidden = requests.post(f"{API_BASE_URL}/internal/reload-model", timeout=5)
    assert forbidden.status_code == 401

    allowed = requests.post(
        f"{API_BASE_URL}/internal/reload-model",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=30,
    )
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["model_loaded"] is True
    assert body["version"].startswith("svd-")


def test_metrics_summary_requires_auth():
    response = requests.get(f"{API_BASE_URL}/metrics/summary", timeout=5)
    assert response.status_code == 401
