"""Últimos tests: caos controlado y límites del borde Nginx.

Se ejecutan al final (nombre ``test_zz_*``) porque abren el Circuit Breaker
y consumen el rate limit de la IP del contenedor de pruebas.
"""

from __future__ import annotations

import json

import requests

from conftest import API_BASE_URL, INTERNAL_TOKEN, NGINX_BASE_URL, auth_headers


def test_demo_bypass_header_avoids_rate_limit(redis_client):
    redis_client.delete("sim:svd_failure")
    session = requests.Session()
    session.headers.update(auth_headers())
    statuses = [
        session.get(f"{NGINX_BASE_URL}/users?limit=1", timeout=10).status_code
        for _ in range(25)
    ]
    assert statuses.count(200) == 25, statuses


def test_global_chaos_flag_degrades_to_fallback(client, redis_client):
    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    # 20 llamadas para cubrir con alta probabilidad los 2 workers de uvicorn.
    for _ in range(20):
        requests.post(
            f"{API_BASE_URL}/internal/reset-circuit-breaker", headers=headers, timeout=5
        )

    for _ in range(20):
        response = requests.post(
            f"{API_BASE_URL}/internal/simulate-failure?enabled=true&latency_ms=0",
            headers=headers,
            timeout=5,
        )
        assert response.status_code == 200

    flag = json.loads(redis_client.get("sim:svd_failure") or "{}")
    assert flag.get("enabled") is True

    degraded = client.get(f"{API_BASE_URL}/recommendations/u_1001?limit=10", timeout=10)
    assert degraded.status_code == 200
    assert degraded.json()["source"] == "fallback"

    for _ in range(20):
        requests.post(
            f"{API_BASE_URL}/internal/reset-circuit-breaker", headers=headers, timeout=5
        )
    flag = json.loads(redis_client.get("sim:svd_failure") or "{}")
    assert flag.get("enabled") is False
    # Se verifica que ambas réplicas quedaron cerradas para no dejar el demo
    # degradado después de la suite.
    states = [
        requests.get(
            f"{API_BASE_URL}/internal/model-status", headers=headers, timeout=5
        ).json()["circuit_breaker"]["state"]
        for _ in range(20)
    ]
    assert set(states) <= {"closed", "half-open"}, states


def test_nginx_enforces_rate_limit():
    session = requests.Session()
    session.headers.update({"X-API-Key": auth_headers()["X-API-Key"], "Authorization": auth_headers()["Authorization"]})
    statuses = [
        session.get(f"{NGINX_BASE_URL}/users?limit=1", timeout=10).status_code
        for _ in range(45)
    ]
    assert 429 in statuses, f"el rate limit de 100 r/m no se aplicó: {set(statuses)}"
    assert 200 in statuses, "el burst=20 nodelay debe permitir ráfagas iniciales"
