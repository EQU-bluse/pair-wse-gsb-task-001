"""Bitemporal query helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PolicyVersion


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_utc(value: datetime) -> datetime:
    """Treat naive timestamps as UTC; return timezone-aware datetimes in UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def find_effective_version(
    session: AsyncSession,
    *,
    policy_id,
    occurred_at: datetime,
    known_at: datetime | None,
) -> PolicyVersion | None:
    """Find the version visible at the intersection of two time axes.

    * Business axis: ``effective_start <= occurred_at < effective_end``
      (NULL ``effective_end`` is open-ended).
    * System axis: ``recorded_at <= known_at`` (defaults to *now*).
    """
    stmt = select(PolicyVersion).where(
        PolicyVersion.policy_id == policy_id,
        PolicyVersion.effective_start <= occurred_at,
        (PolicyVersion.effective_end.is_(None))
        | (PolicyVersion.effective_end > occurred_at),
    )
    if known_at is not None:
        stmt = stmt.where(PolicyVersion.recorded_at <= known_at)
    stmt = stmt.order_by(PolicyVersion.effective_start.desc())
    return await session.scalar(stmt.limit(1))
