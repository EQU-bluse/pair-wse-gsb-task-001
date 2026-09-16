"""Health/readiness checks."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def is_database_ready(
    session_factory: async_sessionmaker[AsyncSession], timeout: float
) -> bool:
    try:
        async with session_factory() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=timeout)
    except Exception:
        return False
    return True
