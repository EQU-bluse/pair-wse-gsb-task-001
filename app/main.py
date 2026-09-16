"""FastAPI application entry point."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.policies import router as policies_router
from app.config import get_settings
from app.db import SessionLocal
from app.errors import register_exception_handlers
from app.logging_config import bind_tenant, configure_logging, get_logger, request_id_var
from app.services.health import is_database_ready

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("app.http")

app = FastAPI(
    title="Multi-tenant Bitemporal Policy Decision Service",
    version="1.0.0",
)
register_exception_handlers(app)


@app.middleware("http")
async def context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request_id_var.set(request_id)
    request.state.request_id = request_id
    tenant = request.headers.get("X-Tenant-ID")
    bind_tenant(tenant)

    if request.url.path not in ("/healthz", "/readyz"):
        logger.info(
            "request started",
            extra={"extra_fields": {"method": request.method, "path": request.url.path}},
        )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request failed")
        raise
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(StarletteHTTPException)
async def _handle_starlette_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Keep one error envelope for framework-level errors (unknown route, etc.).
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": f"http_{exc.status_code}",
                "message": exc.detail if isinstance(exc.detail, str) else "error",
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness: process is up. No dependencies are checked."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz() -> JSONResponse:
    """Readiness: the database must answer a trivial query in time."""
    ready = await is_database_ready(SessionLocal, settings.readiness_timeout)
    if not ready:
        logger.warning("readiness check failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


app.include_router(policies_router)
