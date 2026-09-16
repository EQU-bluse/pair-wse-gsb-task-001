"""Pytest fixtures: real PostgreSQL via the running test container.

The connection URL is taken from ``APP_DATABASE_URL`` (set in the environment
when invoking pytest) before any application module is imported so that the
module-level engine binds to it.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "APP_DATABASE_URL", "postgresql+asyncpg://pds:pds@localhost:5432/pds"
)
os.environ.setdefault("APP_OUTBOX_BASE_DELAY", "0")
os.environ.setdefault("APP_OUTBOX_MAX_DELAY", "0")
os.environ.setdefault("APP_WORKER_POLL_INTERVAL", "0.01")
os.environ.setdefault("APP_READINESS_TIMEOUT", "1.0")

import httpx
import pytest_asyncio
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app

_TABLES = [
    "outbox_events",
    "policy_versions",
    "idempotency_keys",
    "policies",
]


@pytest_asyncio.fixture(autouse=True)
async def truncate_tables():
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(_TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )
    yield
    # pytest-asyncio gives each test function its own event loop; connections
    # checked into the pool keep bindings to a loop that is about to close.
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
