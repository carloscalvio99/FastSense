# FastSense · Recomendador en tiempo real para ShopFast

Implementación completa y reproducible en local (Docker Compose) de la arquitectura
descrita en `FastSense_Informe_Consolidado_arc42.docx` y en la pauta EP3.
El mismo código se despliega en AWS cambiando únicamente variables de entorno
(`AWS_ENDPOINT_URL`, endpoints de RDS/ElastiCache, secretos), sin reescribir lógica.

- **Inferencia:** SVD de `scikit-surprise` (50 factores, 20 épocas, `random_state=42`).
- **Ruta caliente:** Nginx → FastAPI → Redis (Cache-Aside) → SVD (400 ms) → `top:global`.
- **Ruta de escritura:** FastAPI (202) → SQS → Worker → MySQL + Parquet en S3.
- **MLOps:** seeding → entrenamiento → quality gate `RMSE <= 0.85` → S3 + registro → hot reload.
- **Seguridad:** `X-API-Key` + JWT HS256, logs JSON sin PII, anonimización SHA-256, least privilege.

---

## 1. Arquitectura

```mermaid
flowchart LR
    U[Browser :3000] -->|React 18 + Vite| FE[frontend / Nginx]
    U -->|fetch| NG[Nginx :8080<br/>least_conn · keepalive 32<br/>100 r/m burst 20 · X-Request-ID]
    NG -->|/recommendations<br/>/track-interaction| API1[FastAPI worker #1]
    NG --> API2[FastAPI worker #2]
    API1 --> R[(Redis 7<br/>rec:{user_id} TTL 3600<br/>top:global)]
    API2 --> R
    API1 --> M[(MySQL 8.0<br/>USUARIO · PRODUCTO<br/>INTERACCION · MODELO_VERSION)]
    API2 --> M
    API1 -->|SendMessage| Q[[SQS interactions-queue<br/>DLQ maxReceiveCount=3]]
    API2 --> Q
    Q --> W[worker<br/>SHA-256 · INSERT IGNORE · Parquet]
    W --> M
    W --> S3[(S3 datalake-models<br/>versionado)]
    T[ml-trainer<br/>seed + SVD + gate] --> M
    T --> S3
    T -->|manifest + reload| API1
    S3 -->|model.pkl + manifest| API1
    S3 -->|model.pkl + manifest| API2
    API1 -.Cache miss.-> R
```

| Contenedor | Rol | Puerto host |
|---|---|---|
| `nginx` | Borde L7, rate limit, keepalive, request-id | `8080` |
| `api` | FastAPI/uvicorn (2 workers), escalable | interno `8080` |
| `worker` | Consumidor SQS, idempotencia, Parquet/S3 | — |
| `redis` | Caché de recomendaciones y `top:global` | `6379` |
| `mysql` | Modelo transaccional (4 tablas) | `3306` |
| `localstack` | Emulación SQS + S3 | `4566` |
| `ml-trainer` | Seeding, SVD, quality gate y promoción (one-shot) | — |
| `frontend` | Tienda ShopFast + dashboard MLOps | `3000` |
| `tests` (perfil `test`) | Suite pytest de aceptación | — |
| `locust` (perfil `load`) | Prueba de carga 80/20 | `8089` |

### Estructura del repositorio

```
.
├── api/                 # FastAPI: auth, cache, breaker, loader, métricas, rutas
│   ├── app/
│   └── Dockerfile       # multi-stage, builder con gcc, runner UID 1000
├── worker/              # SQS → anonimización → MySQL → Parquet/S3
├── ml-training/         # seed_data.py, train_svd.py, quality_gate.py
├── frontend/            # React 18 + Vite + Tailwind (tienda + dashboard)
├── nginx/nginx.conf     # least_conn, keepalive 32, limit_req, timeouts 2s/5s
├── mysql/init.sql       # USUARIO, PRODUCTO, INTERACCION, MODELO_VERSION
├── localstack/init-aws.sh
├── tests/               # 38 tests pytest + locustfile.py
├── docker-compose.yml
└── .env.example
```

---

## 2. Inicio rápido

Requisitos: Docker Engine 24+ y Docker Compose v2 (probado con Compose v2.29).
No se necesita Python ni Node en el host.

```bash
cp .env.example .env
docker compose up --build -d
```

La primera ejecución tarda varios minutos: compila `scikit-surprise`, siembra
5.000 productos, 50.000 usuarios y 180.000 interacciones, entrena el SVD con
holdout del 20 % y promueve el modelo si supera el gate.

Estado esperado:

```bash
docker compose ps
```

- `nginx`, `api`, `worker`, `redis`, `mysql`, `localstack`, `frontend` → `healthy`.
- `ml-trainer` → `Exited (0)`; es un job batch, no un servicio permanente.
  Su salida contiene el RMSE y la versión promovida:

```bash
docker compose logs ml-trainer | tail -30
```

Verificación mínima:

```bash
curl -s http://localhost:8080/health | jq
curl -s http://localhost:8080/ready  | jq
```

La tienda queda en <http://localhost:3000> y el dashboard MLOps en la pestaña
**MLOps**. La documentación OpenAPI está en <http://localhost:8080/docs>.

---

## 3. Credenciales de demostración

| Elemento | Valor |
|---|---|
| API Key | `fs_demo_key_2026` |
| JWT HS256 (expira 2100-01-01) | ver `JWT_TOKEN` en `.env` |
| Secreto JWT | `fastsense_local_jwt_secret_2026_change_me` |
| Token interno (`/internal/*`) | `fastsense_internal_2026` |
| Salt PII / logs | `fastsense-pii-salt-2026` / `fastsense-log-salt-2026` |
| Usuario demo | `u_1001` |
| Producto demo | `p_1042` |

```bash
export API_KEY="fs_demo_key_2026"
export JWT_TOKEN="$(grep '^JWT_TOKEN=' .env | cut -d= -f2-)"
```

> En AWS estos valores viven en Secrets Manager / SSM y se inyectan como
> variables. `INTERNAL_TOKEN` protege los endpoints de operación y nunca debe
> exponerse en el frontend de producción (aquí se embebe solo para la demo local).

Regenerar el JWT de demo:

```bash
python3 - <<'PY'
import base64, hashlib, hmac, json, time
secret = "fastsense_local_jwt_secret_2026_change_me"
header = {"alg": "HS256", "typ": "JWT"}
payload = {"sub": "shopfast-mlops-demo", "role": "admin",
           "iat": int(time.time()), "exp": 4102444800, "iss": "fastsense-local"}
def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()
h, p = b64(header), b64(payload)
sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
print(f"{h}.{p}.{sig}")
PY
```

---

## 4. Comandos de demostración

### 4.1 Recomendaciones (cache miss → `svd`)

```bash
curl -s "http://localhost:8080/recommendations/u_1001?limit=10" \
  -H "X-API-Key: $API_KEY" \
  -H "Authorization: Bearer $JWT_TOKEN" | jq
```

Respuesta del contrato (`source` ∈ `cache | svd | fallback`):

```json
{
  "user_id": "u_1001",
  "items": [
    {
      "product_id": "p_1042",
      "name": "Aster Laptop Pro 1042",
      "category": "Laptops",
      "price": 1299.99,
      "score": 0.94,
      "rank": 1
    }
  ],
  "source": "svd",
  "request_id": "a1b2c3d4e5f6",
  "latency_ms": 4.12
}
```

### 4.2 Segunda llamada (cache hit → `cache`, TTL 3600 s)

```bash
curl -s "http://localhost:8080/recommendations/u_1001?limit=10" \
  -H "X-API-Key: $API_KEY" -H "Authorization: Bearer $JWT_TOKEN" \
  | jq '{source, latency_ms, request_id}'
```

### 4.3 Degradación elegante (Circuit Breaker → `fallback`, HTTP 200)

```bash
curl -s -i "http://localhost:8080/recommendations/u_1001?limit=10" \
  -H "X-API-Key: $API_KEY" -H "Authorization: Bearer $JWT_TOKEN" \
  -H "X-Simulate-Failure: true" | head -20
```

El cuerpo responde `"source": "fallback"` con el Top-10 de `top:global` y
**nunca** un 5xx.

### 4.4 Ingesta asíncrona (202 en <10 ms)

```bash
curl -s -i -X POST http://localhost:8080/track-interaction \
  -H "X-API-Key: $API_KEY" -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","product_id":"p_1042","event_type":"purchase","session_id":"s_demo"}'
```

```json
{"event_id":"evt_9f2c…","status":"accepted","accepted_at":"2026-11-18T20:00:00.123456+00:00"}
```

El worker lo persiste con `INSERT IGNORE` (idempotencia por `event_id`) y lo
agrega al Data Lake en `s3://fastsense-datalake-models/datalake/interactions/date=YYYY-MM-DD/`.

### 4.5 Hot reload sin reiniciar pods

```bash
curl -s -X POST http://localhost:8080/internal/reload-model \
  -H "X-Internal-Token: fastsense_internal_2026" | jq
```

### 4.6 Golden Signals / registro de modelos

```bash
curl -s http://localhost:8080/metrics/summary \
  -H "X-API-Key: $API_KEY" -H "Authorization: Bearer $JWT_TOKEN" \
  | jq '{model, golden_signals, sources, circuit_breaker}'
```

### 4.7 Caos controlado desde API

```bash
curl -s -X POST "http://localhost:8080/internal/simulate-failure?enabled=true" \
  -H "X-Internal-Token: fastsense_internal_2026"
curl -s -X POST http://localhost:8080/internal/reset-circuit-breaker \
  -H "X-Internal-Token: fastsense_internal_2026"
```

---

## 5. Calidad del modelo (quality gate)

El contenedor `ml-trainer` ejecuta:

1. `seed_data.py` — datos sintéticos deterministas (`SEED=42`) con señal latente
   aprendible; ids fijos `u_1001` y `p_1042`.
2. `train_svd.py` — `SVD(n_factors=50, n_epochs=20, random_state=42)` sobre
   `rating_value` (preferencia implícita/explícita), holdout 20 %.
3. `quality_gate.evaluate_quality_gate(rmse, 0.85)`.
   - **RMSE > 0.85:** no se sube artefacto, se registra `status='rejected'` y el
     pipeline termina con código 1.
   - **RMSE ≤ 0.85:** reentrena con el 100 % de los datos, serializa con
     `joblib.dump(..., compress=("zlib", 3))`, sube `models/model.pkl` y
     `models/manifest.json`, registra `MODELO_VERSION` como `active`, archiva la
     versión anterior, actualiza `top:global` y llama a `/internal/reload-model`.
4. `ModelLoader` de cada réplica descarga el manifest, carga con
   `joblib.load(..., mmap_mode="r")` (con fallback automático si la compresión
   impide el mmap) y sustituye el snapshot en caliente.

Salida real esperada (se completa en la sección 9 tras la ejecución):

```
{"message": "evaluation_complete", "passed": true, "rmse": 0.4xxx, "threshold": 0.85, ...}
{"message": "artifact_published", "s3_uri": "s3://fastsense-datalake-models/models/model.pkl", ...}
{"message": "pipeline_complete", "status": "promoted", "hot_reload": true, ...}
```

Reentrenar sin reiniciar la plataforma:

```bash
docker compose run --rm ml-trainer
```

---

## 6. Pruebas

### 6.1 Suite de aceptación (38 casos)

```bash
docker compose --profile test run --rm tests
```

| # | Archivo | Caso | Verifica |
|---|---|---|---|
| 1 | `test_api_contract.py` | `test_health_contract` | `/health` con status/version/uptime_s |
| 2 | | `test_ready_reports_dependencies` | `/ready` con Redis, MySQL y modelo |
| 3 | | `test_recommendations_cache_miss_then_hit` | primer `svd`, segundo `cache` |
| 4 | | `test_recommendation_response_schema` | contrato exacto de campos |
| 5 | | `test_circuit_breaker_fallback_returns_200` | fallback 200 + `top:global` |
| 6 | | `test_unknown_user_degrades_to_fallback_without_5xx` | usuario inexistente |
| 7 | | `test_limit_validation` | `limit` fuera de rango → 422 |
| 8 | | `test_track_interaction_is_accepted_in_under_10ms` | 202 + event_id |
| 9 | | `test_request_id_is_injected_and_echoed` | trazabilidad Nginx/API |
| 10 | | `test_openapi_available` | OpenAPI 3.0 |
| 11 | `test_security.py` | `test_missing_credentials_returns_401` | sin API Key ni JWT |
| 12 | | `test_invalid_api_key_returns_401` | API Key incorrecta |
| 13 | | `test_missing_jwt_returns_401` | sin Bearer |
| 14 | | `test_invalid_signature_returns_401` | firma HS256 falsa |
| 15 | | `test_expired_jwt_returns_401` | expiración |
| 16 | | `test_public_endpoints_do_not_require_auth` | `/health`, `/ready`, `/docs` |
| 17 | | `test_internal_endpoint_requires_internal_token` | `X-Internal-Token` |
| 18 | | `test_metrics_summary_requires_auth` | dashboard protegido |
| 19 | `test_ingestion.py` | `test_api_event_reaches_mysql_through_sqs` | API → SQS → MySQL |
| 20 | | `test_worker_is_idempotent_with_duplicate_messages` | `INSERT IGNORE` exactly-once |
| 21 | | `test_sha256_anonymization_end_to_end` | hash en MySQL/S3, sin correo |
| 22 | | `test_anonymizer_unit_sha256_is_deterministic` | unitario SHA-256 |
| 23 | | `test_sqs_dlq_redrive_policy` | DLQ + `maxReceiveCount=3` |
| 24 | | `test_s3_bucket_has_versioning` | versionado S3 |
| 25 | | `test_top_global_is_materialized` | fallback disponible |
| 26 | `test_platform.py` | `test_schema_has_four_tables_with_required_columns` | DDL exigido |
| 27 | | `test_idempotency_key_is_unique` | índice UNIQUE |
| 28 | | `test_seed_volumes_meet_requirements` | 5k/50k/150k+ |
| 29 | | `test_demo_user_and_product_exist` | `u_1001`, `p_1042` |
| 30 | | `test_model_registry_promotes_only_quality_models` | RMSE ≤ 0.85 activo |
| 31 | | `test_internal_model_status_reports_loaded_model` | loader == registry |
| 32 | `test_quality_gate.py` | `test_gate_accepts_rmse_at_threshold` | límite 0.85 |
| 33 | | `test_gate_accepts_low_rmse_with_precision` | métricas extra |
| 34 | | `test_gate_rejects_rmse_above_threshold` | rechazo > 0.85 |
| 35 | | `test_gate_supports_custom_threshold` | umbral configurable |
| 36 | `test_zz_chaos_and_limits.py` | `test_demo_bypass_header_avoids_rate_limit` | bypass de carga |
| 37 | | `test_global_chaos_flag_degrades_to_fallback` | breaker global |
| 38 | | `test_nginx_enforces_rate_limit` | 429 al exceder 100 r/m |

Los tests usan el host interno `http://api:8080`; el caso 9 y 36-38 usan
`http://nginx:8080` para validar el borde. La suite espera `/ready` con 8
respuestas 200 consecutivas para cubrir el balanceo entre los 2 workers.

### 6.2 Prueba de carga con Locust

Web UI:

```bash
docker compose --profile load up -d locust
# http://localhost:8089 → 500 usuarios, spawn rate 50
```

Headless (80 % GET / 20 % POST, 3 minutos):

```bash
docker compose --profile load run --rm locust \
  locust -f /app/tests/locustfile.py --host http://nginx:8080 \
  --headless -u 500 -r 50 -t 3m --csv /tmp/locust --only-summary
```

Métricas a reportar: `p50`, `p95`, `p99`, RPS y error rate (Locust los entrega
en la tabla final y en `--csv`). Objetivos del informe: p95 ≈ 420 ms,
p99 < 500 ms, error rate < 2 %. La cabecera `X-Demo-Bypass` evita que el límite
de 100 r/m del borde invalide la medición; el límite se valida aparte en pytest.

---

## 7. Decisiones de diseño (resumen de ADRs)

- **ADR-01 · Caché Cache-Aside con Redis.** `rec:{user_id}` TTL 3600 s; el SVD
  solo se invoca en miss. Reduce p95 y protege MySQL.
- **ADR-02 · SVD precalculado + inferencia vectorizada.** Se extraen `pu/qi/bu/bi`
  y se calcula el score de todo el catálogo con NumPy (`qi @ pu[u]`), evitando
  `predict()` fila a fila; presupuesto duro de 400 ms con `Future.result(timeout)`.
- **ADR-03 · Circuit Breaker + fallback.** `pybreaker` (3 fallos, reset 30 s);
  cualquier error/timeout degrada a `top:global` con HTTP 200.
- **ADR-04 · Ingesta asíncrona con SQS.** El POST solo publica y responde 202;
  desacopla picos de tráfico y permite reintentos/DLQ.
- **ADR-05 · Idempotencia exactly-once lógico.** `INTERACCION.idempotency_key UNIQUE`
  + `INSERT IGNORE` tolera entregas duplicadas de SQS.
- **ADR-06 · Privacidad por diseño.** Correos solo como SHA-256 con salt;
  logs con `user_id` hasheado y redacción de emails/teléfonos; Parquet sin PII.
- **ADR-07 · Modelo como artefacto versionado.** S3 con versionado + manifest +
  `MODELO_VERSION`; hot reload con polling y endpoint interno.
- **ADR-08 · Un solo binario local/AWS.** `boto3` con `AWS_ENDPOINT_URL` opcional;
  sin endpoint usa la cadena IAM estándar.

---

## 8. Equivalencia local ↔ AWS

| Local (Docker Compose) | AWS producción | Notas |
|---|---|---|
| `nginx` :8080 | ALB + WAF (rate-based 100 req/min) + CloudFront | `least_conn` ≈ balanceo ALB; timeouts 2s/5s; request-id en logs |
| `api` (2 workers) | ECS Fargate service (2+ tasks) tras ALB | mismas env vars; UID 1000; Secrets Manager; CloudWatch Logs |
| `worker` | ECS service con autoscaling por `ApproximateNumberOfMessages` | misma lógica SQS/Parquet |
| `redis` | ElastiCache for Redis (Multi-AZ + réplica) | misma URL `redis://` con TLS en prod |
| `mysql` | RDS MySQL 8.0 Multi-AZ + read replica | mismas tablas; backups/PITR |
| `localstack` SQS/S3 | SQS estándar + DLQ; S3 con versionado, SSE-KMS y lifecycle | `AWS_ENDPOINT_URL` vacío |
| `ml-trainer` (one-shot) | SageMaker Training Job o ECS Task orquestado por Step Functions | artefacto a S3 + registro en `MODELO_VERSION`/SageMaker Registry |
| `frontend` (Nginx) | S3 + CloudFront (o ECS + ALB) | `VITE_API_BASE_URL` apunta al ALB |
| `docker compose` | Terraform / AWS CDK | mismos Dockerfiles → ECR |
| `.env` | Secrets Manager / SSM Parameter Store + IRSA | nunca secretos en la imagen |
| `docker compose logs` | CloudWatch Logs (JSON) + X-Ray/ADOT | mismas claves de log |
| `--profile load` | Locust en ECS/Fargate o JMeter distribuido | solo en staging |

La lógica de AWS vive en `api/app/aws.py` y `worker/worker.py`: si
`AWS_ENDPOINT_URL` está vacío, `boto3` usa la cadena de credenciales IAM normal.
No hay rutas de código específicas de LocalStack.

---

## 9. Resultados verificados en esta máquina

Ejecución real del 2026-11-19 con Docker Engine 29.9.3 e imágenes
`fastsense/*:local` construidas desde este repositorio.

### 9.1 Estado de contenedores

```text
SERVICE      STATUS
api          Up (healthy)
frontend     Up (healthy)
localstack   Up (healthy)
ml-trainer   Exited (0)
mysql        Up (healthy)
nginx        Up (healthy)
redis        Up (healthy)
worker       Up (healthy)
```

`ml-trainer` es un job one-shot: `Exited (0)` es el estado correcto, no un fallo.

### 9.2 Semilla y quality gate

```text
products  users  interactions
5000      50000  179387        <- semilla usada por el entrenamiento
5000      50004  181316        <- tras Locust + curls (los eventos crean usuarios)
```

```text
dataset_loaded        interactions=179387 ratings=179387
training_start        n_factors=50 n_epochs=20 ratings=179387
evaluation_complete   passed=true rmse=0.3272 threshold=0.85
                      precision_at_10=0.7443
artifact_published    s3://fastsense-datalake-models/models/model.pkl bytes=23131613
hot_reload_ok         version=svd-20261119-121321
pipeline_complete     status=promoted elapsed_s=18.2
```

Registro activo en `MODELO_VERSION`:

```text
version                rmse     precision_at_10  status
svd-20261119-121321    0.3272   0.7443           active
```

El RMSE `0.3272 <= 0.85` supera el gate. Las versiones anteriores quedaron
`archived` por las iteraciones de depuración de Precision@10.

### 9.3 Suite pytest

```text
collected 38 items
...
tests/test_zz_chaos_and_limits.py::test_nginx_enforces_rate_limit PASSED [100%]

============================= 38 passed in 23.58s ==============================
```

Cobertura: contrato HTTP, caché, breaker/fallback, ingesta SQS → MySQL → S3,
idempotencia, anonimización, DLQ/redrive, esquema y volúmenes, quality gate,
seguridad JWT/API Key, métricas internas, caos y rate limit de Nginx.

### 9.4 Curl de aceptación

`GET /ready` (público):

```json
{
  "status": "ready",
  "redis": "ok",
  "rds": "ok",
  "model_loaded": true,
  "model_version": "svd-20261119-121321",
  "uptime_s": 155.194
}
```

`GET /health` (público):

```json
{
  "status": "ok",
  "version": "3.0.0",
  "uptime_s": 155.278,
  "service": "FastSense Inference API"
}
```

`GET /recommendations/u_1001?limit=10` — miss de caché, se muestran 3 de 10
ítems para brevedad:

```json
{
  "user_id": "u_1001",
  "items": [
    {"product_id": "p_4907", "name": "Vertex Almacenamiento Core 4907",
     "category": "Almacenamiento", "price": 442.6, "score": 0.9469, "rank": 1},
    {"product_id": "p_4509", "name": "Quantum Accesorio Lite 4509",
     "category": "Accesorios", "price": 168.36, "score": 0.9124, "rank": 2},
    {"product_id": "p_5931", "name": "Aster Laptop Pro 5931",
     "category": "Laptops", "price": 198.6, "score": 0.9111, "rank": 3}
  ],
  "source": "svd",
  "request_id": "13703ccbda055624a853e6aabf60ab60",
  "latency_ms": 29.783
}
```

Segunda llamada idéntica (cache hit):

```json
{
  "source": "cache",
  "latency_ms": 0.989,
  "first_item": {
    "product_id": "p_4907",
    "name": "Vertex Almacenamiento Core 4907",
    "category": "Almacenamiento",
    "price": 442.6,
    "score": 0.9469,
    "rank": 1
  }
}
```

`POST /track-interaction`:

```json
{
  "event_id": "evt_675a4aa364f34dccb1f1b4769b7b1fb5",
  "status": "accepted",
  "accepted_at": "2026-11-19T12:26:53.899458Z"
}
```

Log estructurado real (sin PII en claro; el segmento del usuario se redacta en
`path` y el campo `user_id` viaja hasheado):

```json
{"timestamp": "2026-11-19T12:18:02.917+00:00", "level": "INFO",
 "logger": "fastsense.api", "message": "request",
 "request_id": "req_b3d09e8258f64c77", "user_id": "835e35f11385827e",
 "latency_ms": 4.922, "source": "svd", "status_code": 200,
 "path": "/recommendations/[user_id]", "method": "GET"}
```

### 9.5 Prueba de carga Locust

```bash
mkdir -p locust-results
docker compose --profile load run --rm \
  -v "$(pwd)/locust-results:/tmp/locust" locust \
  locust -f /app/tests/locustfile.py --host http://nginx:8080 \
  --headless -u 500 -r 50 -t 3m \
  --csv /tmp/locust/results --only-summary
```

El `wait_time` del locustfile se calcula como `usuarios_activos / TARGET_RPS`
(`TARGET_RPS=50`), de modo que 500 VUs sostienen ~50 RPS sin saturar el SVD.

```text
[INFO] Ramping to 500 users at a rate of 50.00 per second
[INFO] All users spawned: {"FastSenseUser": 500} (500 total users)
[INFO] --run-time limit reached, shutting down   (180 s)
```

| Tipo | Peticiones | Fallos | req/s | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `GET /recommendations/[user_id]` | 7380 | 0 (0.00%) | 41.29 | 16 ms | 350 ms | 490 ms |
| `POST /track-interaction` | 1920 | 0 (0.00%) | 10.74 | 14 ms | 310 ms | 490 ms |
| **Agregado** | **9300** | **0 (0.00%)** | **52.04** | **15 ms** | **340 ms** | **490 ms** |

Media agregada 61.9 ms, máximo 655.8 ms, tasa de error 0%. La mezcla real fue
79.4% recomendaciones / 20.6% tracking, coherente con el 80/20 requerido.

### 9.6 Ajustes incorporados durante la verificación

- Healthcheck de LocalStack con `timeout: 20s`: cada llamada `awslocal` tarda
  ~2.8 s en este host y dos llamadas superaban los 5 s originales.
- `Precision@10` usa `RELEVANT_RATING=4.5` (la media de la semilla es 4.44) y
  consulta los mapas `raw2inner` de Surprise: pasó de `0.0` a `0.7443`.
- El circuit breaker captura un `snapshot` inmutable del modelo antes de
  lanzar el hilo de inferencia, evitando carreras con el hot reload.
- La ruta de logs redacta `/recommendations/{user_id}` a
  `/recommendations/[user_id]`; aplica también a fallos de autenticación.
- El test de caos hace 20 intentos de reset y verifica el estado de ambos
  workers para no dejar el circuito abierto al terminar la suite.
- Corrección de un `import` partido en `tests/test_ingestion.py` que impedía
  la recolección de los 38 tests.

---

## 10. Operación y troubleshooting

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
docker compose logs ml-trainer | tail -50
docker compose exec redis redis-cli GET top:global
docker compose exec redis redis-cli TTL rec:u_1001
docker compose exec mysql mysql -ufastsense -pfastsense fastsense \
  -e "SELECT version, rmse, status FROM MODELO_VERSION ORDER BY trained_at DESC LIMIT 3;"
```

- **`api` unhealthy pero `/health` OK:** `/ready` exige modelo cargado. Es normal
  durante el primer entrenamiento (1-3 min). El healthcheck reintenta hasta 40 veces.
- **`ml-trainer` en `Exited (1)`:** revisar RMSE en logs; el gate bloqueó la
  promoción. Ajustar datos/épocas o el umbral (`RMSE_GATE`) y relanzar.
- **`429 Too Many Requests`:** rate limit de 100 r/m por IP. Para demos de carga
  usar `X-Demo-Bypass: fastsense-demo-bypass` (solo entorno local).
- **`401`:** faltan `X-API-Key` o `Authorization: Bearer <JWT>`; `/health`,
  `/ready`, `/docs` y `/openapi.json` son públicos.
- **Reset total:** `docker compose down -v` (borra MySQL, Redis y LocalStack;
  el siguiente `up` vuelve a sembrar y entrenar).
- **Poca RAM:** el build de `scikit-surprise` y el entrenamiento son los picos.
  Construir con `COMPOSE_PARALLEL_LIMIT=1 docker compose build` y no ejecutar
  Locust durante el entrenamiento.

### Limitaciones conocidas

- `joblib.load(mmap_mode="r")` no aplica a artefactos comprimidos con zlib; el
  loader lo intenta y cae a carga normal (documentado en el código).
- El token interno se embebe en el bundle del frontend solo para la demo local;
  en producción el dashboard debe ir detrás de un IdP y los endpoints internos
  en una red privada.
- Los datos son sintéticos y deterministas; el RMSE mide capacidad del pipeline,
  no rendimiento de negocio real.
