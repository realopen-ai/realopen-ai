# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

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
    libpq5 curl \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# ── Optional dependency overlay ──────────────────────────────────────
# /opt/optional is a Docker volume (see docker-compose.yml). Optional deps
# are extracted here (not installed to system paths), so they survive 
# container rebuilds without re-downloading or re-extracting.
#
# We add /opt/optional/usr/bin to PATH and the lib dirs to LD_LIBRARY_PATH
# so binaries and libraries in the overlay are found automatically.
RUN mkdir -p /opt/optional

ENV PATH="/opt/optional/usr/bin:/opt/optional/usr/sbin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/optional/usr/lib/x86_64-linux-gnu:/opt/optional/usr/lib:/opt/optional/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH}"
ENV PYTHONPATH="/opt/optional/usr/lib/python3/dist-packages:${PYTHONPATH}"

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --loop uvloop --http httptools"]