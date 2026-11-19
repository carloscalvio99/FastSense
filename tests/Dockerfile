# =====================================================================
# FastSense · imagen de pruebas de aceptación y carga
# Se construye desde la raíz para reutilizar anonymizer/quality_gate.
# =====================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/lib

WORKDIR /app

COPY tests/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY tests /app/tests
COPY worker/anonymizer.py /app/lib/anonymizer.py
COPY ml-training/quality_gate.py /app/lib/quality_gate.py

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/tests/.pytest_cache \
    && chown -R appuser:appuser /app

USER appuser

CMD ["pytest", "-v", "/app/tests"]
