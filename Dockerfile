# Industrial IoT Data Hub - container image (Linux). See docs/backend.md section 26.
#
#   docker compose up -d                  # hub + PostgreSQL/TimescaleDB (docker-compose.yml)
#
# Configuration is mounted at /app/config, secrets come from the environment (.env next to
# docker-compose.yml), logs/backups/pki are volumes. The image runs as an unprivileged user.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HUB_HOME=/app

# pg_dump/pg_restore for the nightly backups and restore tests (client only)
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# dependencies first (cached layer): exactly the locked versions
COPY requirements.lock ./
RUN pip install --upgrade pip && pip install -r requirements.lock

COPY pyproject.toml README.md CLAUDE.md .env.example ./
COPY hub ./hub
COPY backend ./backend
COPY simulators ./simulators
COPY frontend ./frontend
COPY db ./db
COPY docs ./docs
COPY config ./config
RUN pip install --no-deps -e . \
    && useradd --system --uid 10001 --home-dir /app hub \
    && mkdir -p /app/logs /app/backups /app/pki /app/spool \
    && chown -R hub:hub /app/logs /app/backups /app/pki /app/spool

USER hub
EXPOSE 8000
ENV HUB_HOST=0.0.0.0 \
    HUB_PORT=8000 \
    HUB_CONFIG_DIR=/app/config \
    HUB_LOG_FILE=/app/logs/hub.log \
    HUB_BACKUP_DIR=/app/backups \
    HUB_PKI_DIR=/app/pki \
    HUB_SPOOL_DIR=/app/spool

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["hub-api"]
