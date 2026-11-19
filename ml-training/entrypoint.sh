#!/usr/bin/env bash
# Pipeline batch: semilla idempotente -> entrenamiento -> quality gate -> deploy.
set -euo pipefail

echo "[ml-trainer] $(date -u +%FT%TZ) iniciando pipeline"

python -u seed_data.py
python -u train_svd.py

echo "[ml-trainer] $(date -u +%FT%TZ) pipeline finalizado correctamente"
