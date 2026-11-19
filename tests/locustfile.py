"""Prueba de carga FastSense (Locust).

Mezcla del informe: 80% GET /recommendations, 20% POST /track-interaction.
Objetivo: hasta 500 usuarios virtuales / ~50 RPS sostenidos.

Uso headless (recomendado):
  docker compose --profile load run --rm locust \
    locust -f /app/tests/locustfile.py --host http://nginx:8080 \
    --headless -u 500 -r 50 -t 3m --csv /tmp/locust --only-summary

Uso con UI:  docker compose --profile load up -d locust  ->  http://localhost:8089
La cabecera X-Demo-Bypass evita que el rate limit de 100 r/m del borde
distorsione la medición; el límite se valida en pytest.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timezone

from locust import HttpUser, task

API_KEY = os.getenv("API_KEY", "fs_demo_key_2026")
JWT_TOKEN = os.getenv("JWT_TOKEN", "")
TARGET_RPS = float(os.getenv("TARGET_RPS", "50"))

USER_POOL = [f"u_{1001 + index}" for index in range(2000)]
PRODUCT_POOL = [f"p_{1001 + index}" for index in range(5000)]
EVENT_TYPES = ["click", "click", "click", "cart", "purchase", "rating"]


class FastSenseUser(HttpUser):
    """Mezcla 80/20 con ritmo agregado objetivo (~50 RPS a 500 VUs)."""

    def wait_time(self) -> float:
        # Con N usuarios activos, cada uno espera N/TARGET_RPS segundos para
        # que la suma se mantenga cerca de TARGET_RPS sin importar el escalado.
        runner = self.environment.runner
        users = runner.user_count if runner and runner.user_count else 1
        return max(0.2, users / TARGET_RPS)

    def on_start(self) -> None:
        self.headers = {
            "X-API-Key": API_KEY,
            "Authorization": f"Bearer {JWT_TOKEN}",
            "X-Demo-Bypass": "fastsense-demo-bypass",
        }
        self.client.headers.update(self.headers)

    @task(4)
    def get_recommendations(self) -> None:
        user_id = random.choice(USER_POOL)
        with self.client.get(
            f"/recommendations/{user_id}?limit=10",
            name="/recommendations/[user_id]",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            try:
                payload = response.json()
            except ValueError:
                response.failure("respuesta no JSON")
                return
            if payload.get("source") not in {"cache", "svd", "fallback"}:
                response.failure(f"source inválido: {payload.get('source')}")
            elif not payload.get("items"):
                response.failure("sin ítems recomendados")

    @task(1)
    def track_interaction(self) -> None:
        event_type = random.choice(EVENT_TYPES)
        body = {
            "user_id": random.choice(USER_POOL),
            "product_id": random.choice(PRODUCT_POOL),
            "event_type": event_type,
            "session_id": f"s_locust_{uuid.uuid4().hex[:12]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if event_type == "rating":
            body["rating"] = round(random.uniform(1.0, 5.0), 1)
        with self.client.post(
            "/track-interaction",
            json=body,
            name="/track-interaction",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"HTTP {response.status_code}")
            elif "event_id" not in (response.json() or {}):
                response.failure("respuesta sin event_id")
