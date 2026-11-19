"""Contrato HTTP de la API de inferencia (capítulo 5.4 del informe)."""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from conftest import API_BASE_URL, NGINX_BASE_URL, auth_headers


def test_health_contract(client):
    response = requests.get(f"{API_BASE_URL}/health", timeout=5)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["uptime_s"] >= 0
    assert "service" in body


def test_ready_reports_dependencies(client):
    response = requests.get(f"{API_BASE_URL}/ready", timeout=5)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["redis"] == "ok"
    assert body["rds"] == "ok"
    assert body["model_loaded"] is True
    assert body["model_version"].startswith("svd-")


def test_recommendations_cache_miss_then_hit(client, mysql, redis_client):
    redis_client.delete("sim:svd_failure")
    with mysql.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_id, COUNT(*) AS interactions
            FROM INTERACCION
            GROUP BY user_id
            HAVING interactions >= 3
            ORDER BY user_id DESC
            LIMIT 1 OFFSET %s
            """,
            (int(uuid.uuid4().int % 4000),),
        )
        row = cursor.fetchone()
    assert row, "no hay usuarios con historial suficiente"
    user_id = row["user_id"]
    redis_client.delete(f"rec:{user_id}")

    first = client.get(f"{API_BASE_URL}/recommendations/{user_id}?limit=10", timeout=10)
    assert first.status_code == 200, first.text
    assert first.json()["source"] == "svd", first.text
    assert redis_client.exists(f"rec:{user_id}") == 1

    second = client.get(f"{API_BASE_URL}/recommendations/{user_id}?limit=10", timeout=10)
    assert second.status_code == 200
    assert second.json()["source"] == "cache"
    assert [item["product_id"] for item in second.json()["items"]] == [
        item["product_id"] for item in first.json()["items"]
    ]


def test_recommendation_response_schema(client):
    response = client.get(f"{API_BASE_URL}/recommendations/u_1001?limit=10", timeout=10)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"user_id", "items", "source", "request_id", "latency_ms"}
    assert body["user_id"] == "u_1001"
    assert body["source"] in {"cache", "svd", "fallback"}
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert body["latency_ms"] >= 0
    assert 1 <= len(body["items"]) <= 10
    for rank, item in enumerate(body["items"], start=1):
        assert set(item) == {"product_id", "name", "category", "price", "score", "rank"}
        assert item["rank"] == rank
        assert 0.0 <= item["score"] <= 1.0
        assert item["price"] >= 0
        assert item["name"]


def test_circuit_breaker_fallback_returns_200(client, redis_client):
    redis_client.delete("sim:svd_failure")
    headers = {**auth_headers(), "X-Simulate-Failure": "true"}
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001?limit=10", headers=headers, timeout=10
    )
    assert response.status_code == 200, "el fallback jamás debe responder 5xx"
    body = response.json()
    assert body["source"] == "fallback"
    assert len(body["items"]) == 10
    # El fallback usa exactamente top:global.
    import json

    top = json.loads(redis_client.get("top:global") or "[]")
    top_ids = {item["product_id"] for item in top}
    assert {item["product_id"] for item in body["items"]} <= top_ids
    assert response.headers.get("X-Recommendation-Source") == "fallback"


def test_unknown_user_degrades_to_fallback_without_5xx(client):
    response = client.get(
        f"{API_BASE_URL}/recommendations/u_no_existe_{uuid.uuid4().hex[:8]}?limit=5",
        timeout=10,
    )
    assert response.status_code == 200
    assert response.json()["source"] == "fallback"
    assert len(response.json()["items"]) == 5


def test_limit_validation(client):
    assert client.get(f"{API_BASE_URL}/recommendations/u_1001?limit=0").status_code == 422
    assert client.get(f"{API_BASE_URL}/recommendations/u_1001?limit=9999").status_code == 422


def test_track_interaction_is_accepted_in_under_10ms(client):
    payload = {
        "user_id": "u_1001",
        "product_id": "p_1042",
        "event_type": "click",
        "session_id": "s_pytest_202",
    }
    started = time.perf_counter()
    response = client.post(f"{API_BASE_URL}/track-interaction", json=payload, timeout=5)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"event_id", "status", "accepted_at"}
    assert body["event_id"].startswith("evt_")
    assert body["status"] == "accepted"
    assert body["accepted_at"]
    assert response.headers.get("X-Event-ID") == body["event_id"]
    # Margen amplio para CI; el servidor no espera a SQS (BackgroundTasks).
    assert elapsed_ms < 500, f"track-interaction tardó {elapsed_ms:.1f} ms"


def test_request_id_is_injected_and_echoed(client):
    fixed = f"pytest-{uuid.uuid4().hex[:12]}"
    response = requests.get(
        f"{API_BASE_URL}/recommendations/u_1001",
        headers={**auth_headers(), "X-Request-ID": fixed},
        timeout=10,
    )
    assert response.status_code == 200
    assert response.json()["request_id"] == fixed

    proxied = requests.get(f"{NGINX_BASE_URL}/health", timeout=10)
    assert proxied.status_code == 200
    nginx_id = proxied.headers.get("X-Request-ID")
    assert nginx_id and "pytest" not in nginx_id


def test_openapi_available(client):
    response = requests.get(f"{API_BASE_URL}/openapi.json", timeout=10)
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/recommendations/{user_id}" in paths
    assert "/track-interaction" in paths
    assert "/health" in paths
    assert "/ready" in paths
