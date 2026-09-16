"""Async SQLAlchemy engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    echo=_settings.db_echo,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _set_timezone(dbapi_connection, _connection_record) -> None:
    # All timestamp arithmetic is performed in UTC.
    cursor = dbapi_connection.cursor()
    cursor.execute("SET TIME ZONE 'UTC'")
    cursor.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
