"""FastAPI application factory."""

from __future__ import annotations

import logging

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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if (services.role != "active" and path.startswith("/api/")
                and path not in ("/api/health", "/api/docs", "/api/openapi.json")):
            # HA standby: no data, no logins, no writes; the load balancer uses the other node
            response = JSONResponse({"error": "standby", "message": f"node is {services.role}"},
                                    status_code=503)
        else:
            response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
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
