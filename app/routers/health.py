from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine

router = APIRouter(tags=["health"])


async def check_db() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    try:
        await check_db()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error": "database is not reachable"},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
