"""Health and readiness checks, including database outage."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.health import is_database_ready


async def test_liveness(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok(client):
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_fails_when_database_unavailable():
    # Point an engine at a closed local port; the readiness probe must give up
    # and report "not ready" instead of raising.
    url = os.environ["APP_DATABASE_URL"].replace("@localhost:5432", "@127.0.0.1:5499")
    engine = create_async_engine(url, pool_pre_ping=False)
    factory = async_sessionmaker(engine)
    try:
        ready = await is_database_ready(factory, timeout=1.0)
        assert ready is False
    finally:
        await engine.dispose()
