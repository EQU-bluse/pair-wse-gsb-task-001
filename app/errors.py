"""Domain errors and the unified error-response machinery."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class NotFound(AppError):
    def __init__(self, resource: str = "resource") -> None:
        # Same body for "missing" and "belongs to another tenant": the service
        # never reveals whether a resource exists for a different tenant.
        super().__init__(status.HTTP_404_NOT_FOUND, "not_found", f"{resource} not found")


class Conflict(AppError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_409_CONFLICT, code, message, details=details)


class PreconditionFailed(AppError):
    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        details = {"current_revision": current_revision} if current_revision is not None else {}
        super().__init__(
            status.HTTP_412_PRECONDITION_FAILED,
            "precondition_failed",
            message,
            details=details,
        )


class UnprocessableRule(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(422, "invalid_rule", message)


def _error_body(code: str, message: str, request: Request, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, request, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic v2 nests the original ValueError under ctx["error"]; coerce
        # such objects to strings so the envelope stays JSON-serializable.
        details = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "validation_error", "request validation failed", request, details
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _handle_integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        # Defensive: services translate expected constraint failures to
        # Conflict/UnprocessableRule before they surface here.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_error_body("integrity_error", "data integrity violation", request),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "internal server error", request),
        )
