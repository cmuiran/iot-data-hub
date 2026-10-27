"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import (
    auth,
    calc,
    config,
    control,
    dashboards,
    data,
    metrics,
    model,
    reports,
    schedules,
    system,
    users,
    ws,
)
from backend.errors import ApiError
from backend.guards import NetworkAllowlist, RateLimiter
from backend.services import Services

log = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# The dashboard: only its own files, no inline script or style, live data over this host's WebSocket
_UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
           "connect-src 'self' {ws}; font-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'self'; frame-ancestors 'none'")


def _ui_csp(request: Request) -> str:
    host = request.headers.get("host", "")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return _UI_CSP.format(ws=f"{scheme}://{host}" if host else "")


API_VERSION = "1"
SUPPORTED_VERSIONS = ("1",)
_VERSIONED = re.compile(r"^/api/v(\d+)(/.*)?$")


class ApiVersions:
    """``/api/v1/...`` is the stable, versioned API; ``/api/...`` is an alias of the current
    version (used by the bundled dashboard). Every API answer carries ``API-Version``; an
    unknown version is refused with ``unsupported_api_version`` instead of a confusing 404.
    A future v2 adds its routes while v1 keeps answering for existing integrations."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        match = _VERSIONED.match(path)
        if match and match[1] not in SUPPORTED_VERSIONS:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4404})
                return
            body = json.dumps({"error": "unsupported_api_version",
                               "message": f"API v{match[1]} is not available; supported: "
                                          + ", ".join(f"v{v}" for v in SUPPORTED_VERSIONS)}
                              ).encode()
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"api-version", API_VERSION.encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if match:
            new = "/api" + (match[2] or "")
            scope = dict(scope, path=new, raw_path=new.encode())
        if scope["type"] == "http" and scope["path"].startswith("/api"):
            async def send_with_version(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    message = dict(message, headers=[*message.get("headers", []),
                                                     (b"api-version", API_VERSION.encode())])
                await send(message)

            await self.app(scope, receive, send_with_version)
            return
        await self.app(scope, receive, send)


def create_app(services: Services) -> FastAPI:
    """The services are started/stopped by the caller (backend.server or a test)."""
    docs = services.settings.api_docs
    app = FastAPI(title="Industrial IoT Data Hub", version=system.VERSION,
                  docs_url="/api/docs" if docs else None,
                  openapi_url="/api/openapi.json" if docs else None, redoc_url=None)
    app.state.services = services

    if services.settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=services.settings.cors_origins,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"])

    allowlist = NetworkAllowlist(services.settings.allowed_networks)
    limiter = RateLimiter(services.settings.rate_limit_per_min)
    app.state.limiter = limiter

    @app.middleware("http")
    async def guards(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        addr = request.client.host if request.client else None
        guarded = path.startswith("/api/") or path == "/metrics"
        if guarded and path != "/api/health" and not allowlist.allows(addr):
            log.warning("request from %s refused: not in HUB_ALLOWED_NETWORKS", addr)
            response: Any = JSONResponse({"error": "network_not_allowed",
                                          "message": "this address may not use the hub"},
                                         status_code=403)
        elif guarded and path != "/api/health" and (wait := limiter.check(addr or "-")):
            response = JSONResponse({"error": "rate_limited",
                                     "message": "too many requests", "retry_after_s": wait},
                                    status_code=429, headers={"Retry-After": str(wait)})
        elif (services.role != "active" and path.startswith("/api/")
              and path not in ("/api/health", "/api/docs", "/api/openapi.json")):
            # HA standby: no data, no logins, no writes; the load balancer uses the other node
            response = JSONResponse({"error": "standby", "message": f"node is {services.role}"},
                                    status_code=503)
        else:
            response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        ui = not guarded and not path.startswith("/api/")
        # API answers are never cached; dashboard files are revalidated (ETag) on every load
        response.headers.setdefault("Cache-Control", "no-cache" if ui else "no-store")
        if ui:
            response.headers.setdefault("Content-Security-Policy", _ui_csp(request))
        elif not path.startswith("/api/docs"):  # Swagger UI needs its scripts
            response.headers.setdefault("Content-Security-Policy",
                                        "default-src 'none'; frame-ancestors 'none'")
        if services.settings.tls:
            response.headers.setdefault("Strict-Transport-Security",
                                        "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        if exc.status == 429 and "retry_after_s" in exc.extra:
            headers = {"Retry-After": str(exc.extra["retry_after_s"])}
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "invalid", "message": "request validation failed",
                             "details": details}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"error": code, "message": str(exc.detail)},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse({"error": "internal", "message": "internal server error"},
                            status_code=500)

    for module in (auth, users, data, control, config, model, calc, dashboards, reports,
                   schedules, system, metrics, ws):
        app.include_router(module.router)
    _mount_frontend(app, services.settings.frontend_dir)
    app.add_middleware(ApiVersions)   # outermost: /api/v1 is mapped before the guards run
    return app


class _FrontendFiles(StaticFiles):
    """StaticFiles that answers 404 for names the file system rejects (on Windows ``:``,
    ``*``, ``?``, ... raise OSError inside the lookup, which would otherwise be a 500)."""

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        try:
            return super().lookup_path(path)
        except (OSError, ValueError):
            return "", None


def _mount_frontend(app: FastAPI, directory: Path | None) -> None:
    """Serve the dashboard at / (hash routing, so no server-side fallback is needed)."""
    if directory is None:
        return
    if not (directory / "index.html").is_file():
        log.warning("HUB_FRONTEND_DIR %s has no index.html; serving the API only", directory)
        return
    app.mount("/", _FrontendFiles(directory=directory, html=True), name="frontend")
