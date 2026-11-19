"""Plataforma de datos: esquema MySQL, volúmenes, registro de modelos."""

from __future__ import annotations

import os

import requests

from conftest import API_BASE_URL, INTERNAL_TOKEN, MYSQL


EXPECTED_COLUMNS = {
    "USUARIO": {"user_id", "email_hash", "created_at", "segment"},
    "PRODUCTO": {"product_id", "name", "category", "price", "stock", "attributes"},
    "INTERACCION": {
        "interaction_id",
        "user_id",
        "product_id",
        "event_type",
        "rating_value",
        "timestamp",
        "session_id",
        "idempotency_key",
        "created_at",
    },
    "MODELO_VERSION": {
        "model_id",
        "version",
        "rmse",
        "precision_at_10",
        "s3_uri",
        "trained_at",
        "status",
    },
}


def test_schema_has_four_tables_with_required_columns(mysql):
    with mysql.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s",
            (MYSQL["database"],),
        )
        rows = cursor.fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])
    for table, columns in EXPECTED_COLUMNS.items():
        assert table in found, f"falta la tabla {table}"
        missing = columns - found[table]
        assert not missing, f"{table} sin columnas {missing}"


def test_idempotency_key_is_unique(mysql):
    with mysql.cursor() as cursor:
        cursor.execute(
            "SELECT INDEX_NAME, NON_UNIQUE FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'INTERACCION' "
            "AND COLUMN_NAME = 'idempotency_key'",
            (MYSQL["database"],),
        )
        indexes = cursor.fetchall()
    assert indexes, "idempotency_key debe estar indexado"
    assert any(int(index["NON_UNIQUE"]) == 0 for index in indexes), "el índice debe ser UNIQUE"


def test_seed_volumes_meet_requirements(mysql):
    with mysql.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS c FROM PRODUCTO")
        products = int(cursor.fetchone()["c"])
        cursor.execute("SELECT COUNT(*) AS c FROM USUARIO")
        users = int(cursor.fetchone()["c"])
        cursor.execute("SELECT COUNT(*) AS c FROM INTERACCION")
        interactions = int(cursor.fetchone()["c"])
    assert products >= 5_000, f"productos={products}"
    assert users >= 50_000, f"usuarios={users}"
    assert interactions > 150_000, f"interacciones={interactions}"


def test_demo_user_and_product_exist(mysql):
    with mysql.cursor() as cursor:
        cursor.execute("SELECT user_id FROM USUARIO WHERE user_id = 'u_1001'")
        assert cursor.fetchone(), "falta el usuario de la demo u_1001"
        cursor.execute("SELECT product_id, name FROM PRODUCTO WHERE product_id = 'p_1042'")
        product = cursor.fetchone()
        assert product, "falta el producto de la demo p_1042"
        assert product["name"]
        cursor.execute(
            "SELECT COUNT(*) AS c FROM INTERACCION WHERE user_id = 'u_1001' "
            "AND product_id = 'p_1042'"
        )
        assert int(cursor.fetchone()["c"]) >= 1


def test_model_registry_promotes_only_quality_models(mysql):
    with mysql.cursor() as cursor:
        cursor.execute(
            "SELECT version, rmse, precision_at_10, status FROM MODELO_VERSION "
            "WHERE status = 'active' ORDER BY trained_at DESC LIMIT 1"
        )
        model = cursor.fetchone()
    assert model, "no hay modelo activo en MODELO_VERSION"
    assert model["version"].startswith("svd-")
    assert float(model["rmse"]) <= 0.85, f"RMSE={model['rmse']} viola el gate"
    assert 0.0 <= float(model["precision_at_10"]) <= 1.0


def test_internal_model_status_reports_loaded_model(client):
    response = requests.get(
        f"{API_BASE_URL}/internal/model-status",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=10,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["loader"]["model_loaded"] is True
    assert body["loader"]["rmse"] <= 0.85
    assert body["registry"]["version"] == body["loader"]["version"]
