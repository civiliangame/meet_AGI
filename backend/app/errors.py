"""Uniform error responses.

Every non-2xx response from `/api` has the shape documented in the contract:

    {"error": {"code": "not_found", "message": "...", "detail": {...}}}

FastAPI's default `{"detail": ...}` shape does not match, so the handlers registered
in `main.py` rewrite validation and HTTP errors into this envelope. The frontend can
therefore have exactly one error parser.
"""

from __future__ import annotations

from fastapi import HTTPException, status


class ApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.extra = detail


def not_found(resource: str, resource_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "not_found",
        f"{resource} {resource_id} not found",
        {"resource": resource, "id": resource_id},
    )


def bad_request(message: str, detail: dict[str, object] | None = None) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, "bad_request", message, detail)


def conflict(message: str, detail: dict[str, object] | None = None) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "conflict", message, detail)
