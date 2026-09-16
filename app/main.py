import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.errors import AppError
from app.logging import configure_logging, get_logger, request_id_var, tenant_id_var
from app.routers import health, policies

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("app")

app = FastAPI(title="Policy Decision Service", version="0.1.0")


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    tenant_id = request.headers.get("X-Tenant-ID")
    request_id_var.set(request_id)
    tenant_id_var.set(tenant_id)
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        tenant_id=tenant_id,
        method=request.method,
        path=request.url.path,
    )
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("method", "path")
    response.headers["X-Request-ID"] = request_id
    return response


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": request_id_var.get(),
    }


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info("request_failed", code=exc.code, status_code=exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body(
            "VALIDATION_ERROR",
            "request validation failed",
            {"errors": jsonable_encoder(exc.errors())},
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }.get(exc.status_code, f"HTTP_{exc.status_code}")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, str(exc.detail)),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception")
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "internal server error"),
    )


app.include_router(health.router)
app.include_router(policies.router)
