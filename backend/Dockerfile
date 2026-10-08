# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

ENV HF_HOME=/app/data/huggingface \
    REALOPEN_DATA_DIR=/app/data \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry

# Configure poetry: no virtualenv (install into system python)
RUN poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-interaction --no-ansi --no-root

# Stage 2: Production image
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl tesseract-ocr tesseract-ocr-eng \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN rm -f /etc/apt/apt.conf.d/docker-clean

RUN mkdir -p /opt/optional

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

# --ws-ping-interval 3600: ASR inference (GIL-heavy, seconds-long on CPU)
# starves the event loop; uvicorn's default 20s WS keepalive then kills
# LIVE voice sessions with 1011 "keepalive ping timeout". The voice
# protocol has its own app-level ping (25 s) + a 600 s idle watchdog, so
# the transport-level keepalive is effectively disabled (3600 s).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --loop uvloop --http httptools --ws-ping-interval 3600 --ws-ping-timeout 3600"]
