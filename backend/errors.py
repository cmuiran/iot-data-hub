"""API errors: every error response is ``{"error": "<code>", "message": "..."}``.

The frontend translates ``error`` codes (fa/en); ``message`` is English detail for logs.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str = "", *, code: str | None = None, status: int | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if status:
            self.status = status
        self.extra = extra or {}

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class Unauthorized(ApiError):
    status, code = 401, "unauthorized"


class Forbidden(ApiError):
    status, code = 403, "forbidden"


class NotFound(ApiError):
    status, code = 404, "not_found"


class Conflict(ApiError):
    status, code = 409, "conflict"


class Invalid(ApiError):
    status, code = 422, "invalid"


class Unavailable(ApiError):
    status, code = 503, "unavailable"


class TooManyRequests(ApiError):
    status, code = 429, "too_many_requests"
