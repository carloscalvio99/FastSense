# =====================================================================
# FastSense API · Dockerfile multi-stage de producción
#  builder: compila scikit-surprise (C/Cython) con numpy 1.26.4
#  runner : python:3.11-slim + usuario appuser (UID 1000) + uvicorn
# =====================================================================
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CONSTRAINT=/app/constraints.txt

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt constraints.txt ./

# numpy/Cython se instalan primero y el build de scikit-surprise corre sin
# aislamiento para que use exactamente numpy 1.26.4 (numpy 2.x rompe el ABI).
RUN pip install --user --no-cache-dir \
        "Cython<3.0" \
        "numpy==1.26.4" \
        "setuptools<70" \
        "wheel" \
    && pip install --user --no-cache-dir --no-build-isolation "scikit-surprise==1.1.3" \
    && pip install --user --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------
FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/home/appuser/.local/bin:$PATH \
    MODEL_DIR=/tmp/fastsense-models

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /root/.local /home/appuser/.local
COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 8080

# Liveness + readiness reales (Redis, MySQL y modelo cargado).
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=40 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2); urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--no-access-log"]
