# =====================================================================
# FastSense · pipeline batch MLOps (seeding + training + promotion)
# Contenedor one-shot: termina con código 0 tras promover el modelo.
# =====================================================================
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CONSTRAINT=/app/constraints.txt

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt constraints.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install "Cython<3.0" "numpy==1.26.4" "setuptools<70" wheel \
    && /opt/venv/bin/pip install --no-build-isolation "scikit-surprise==1.1.3" \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------
FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    MPLBACKEND=Agg

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /tmp/artifacts && chown -R appuser:appuser /tmp/artifacts

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser seed_data.py train_svd.py quality_gate.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

USER appuser

CMD ["./entrypoint.sh"]
