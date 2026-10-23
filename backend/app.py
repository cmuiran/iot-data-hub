"""FastAPI application factory."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import (
    auth,
    config,
    control,
    dashboards,
    data,
    metrics,
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
    "Cache-Control": "no-store",
}


def create_app(services: Services) -> FastAPI:
    """The services are started/stopped by the caller (backend.server or a test)."""
    app = FastAPI(title="Industrial IoT Data Hub", version=system.VERSION,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
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
        if not path.startswith("/api/docs"):  # Swagger UI needs its scripts
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

    for module in (auth, users, data, control, config, dashboards, reports, schedules, system,
                   metrics, ws):
        app.include_router(module.router)
    return app
