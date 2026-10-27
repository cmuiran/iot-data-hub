# The hub (API, dashboard, consumer app, drivers) as a container for a cloud deployment.
# Build from the repository root:  docker build -f deploy/Dockerfile -t iot-data-hub .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HUB_HOST=0.0.0.0 HUB_PORT=8000 HUB_CONFIG_DIR=/srv/hub/config HUB_SPOOL_DIR=/srv/hub/spool

WORKDIR /srv/hub
COPY pyproject.toml ./
COPY hub ./hub
COPY backend ./backend
COPY simulators ./simulators
COPY db ./db
COPY frontend ./frontend
COPY app ./app
RUN pip install . \
    && useradd --system --uid 10001 --home /srv/hub hub \
    && mkdir -p /srv/hub/config/devices /srv/hub/spool /srv/hub/pki \
    && chown -R hub:hub /srv/hub
USER hub

EXPOSE 8000 7000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# migrations are applied at start-up by hub-api (hub.storage.migrate)
CMD ["hub-api"]
