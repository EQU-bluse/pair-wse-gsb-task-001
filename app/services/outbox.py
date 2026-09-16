"""Outbox claiming, delivery and retry bookkeeping.

Multiple worker instances coordinate through PostgreSQL row locks: due events
are selected ``FOR UPDATE SKIP LOCKED`` inside the claiming transaction and
atomically flipped to ``processing``, so each event is delivered by at most
one live worker. Failed deliveries use exponential backoff until
``max_attempts`` is exhausted, after which the event is parked in
``dead_letter``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.models import OutboxEvent
from app.services.time import utcnow

logger = get_logger("app.outbox")

Sender = Callable[[OutboxEvent], Awaitable[None]]

_STALE_CLAIM = timedelta(minutes=5)
_BATCH_SIZE = 10


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def backoff_delay(attempts: int, base_delay: float, max_delay: float) -> float:
    return min(max_delay, base_delay * (2 ** (attempts - 1)))


async def default_sender(event: OutboxEvent) -> None:
    """Production "send": emit a structured delivery log entry."""
    logger.info(
        "outbox event delivered",
        extra={
            "extra_fields": {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "tenant_id": event.tenant_id,
                "aggregate": f"{event.aggregate_type}:{event.aggregate_id}",
                "attempts": event.attempts,
                "payload": event.payload,
            }
        },
    )


async def claim_due(
    session: AsyncSession,
    worker_id: str,
    *,
    batch_size: int = _BATCH_SIZE,
    stale_after: timedelta = _STALE_CLAIM,
) -> list[OutboxEvent]:
    """Atomically claim due events.

    Candidates are either fresh ``pending`` rows whose ``available_at`` has
    arrived, or ``processing`` rows whose worker presumably died (claim older
    than ``stale_after``). ``SKIP LOCKED`` makes concurrent workers take
    disjoint batches without blocking.
    """
    now = utcnow()
    stale_cutoff = now - stale_after

    # A single UPDATE ... RETURNING statement: the FOR UPDATE SKIP LOCKED
    # subquery picks and locks this worker's batch, and the outer UPDATE flips
    # the same rows in one atomic operation. Splitting the two would re-evaluate
    # the candidate query after the status flip and claim nothing.
    candidate_ids = (
        select(OutboxEvent.id)
        .where(
            (
                (OutboxEvent.status == "pending")
                & (OutboxEvent.available_at <= now)
            )
            | (
                (OutboxEvent.status == "processing")
                & (OutboxEvent.locked_at < stale_cutoff)
            )
        )
        .order_by(OutboxEvent.available_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    claim_stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(candidate_ids))
        .values(
            status="processing",
            locked_by=worker_id,
            locked_at=now,
            attempts=OutboxEvent.attempts + 1,
        )
        .returning(OutboxEvent)
    )
    result = await session.scalars(claim_stmt)
    return list(result)


async def mark_sent(session: AsyncSession, event: OutboxEvent) -> None:
    event.status = "sent"
    event.locked_by = None
    event.locked_at = None
    event.last_error = None
    event.sent_at = utcnow()
    await session.flush()


async def mark_failure(
    session: AsyncSession,
    event: OutboxEvent,
    error: str,
    *,
    base_delay: float,
    max_delay: float,
) -> str:
    """Apply retry/backoff or move to dead-letter. Returns new status."""
    if event.attempts >= event.max_attempts:
        event.status = "dead_letter"
        event.locked_by = None
        event.locked_at = None
    else:
        delay = backoff_delay(event.attempts, base_delay, max_delay)
        event.status = "pending"
        event.available_at = utcnow() + timedelta(seconds=delay)
        event.locked_by = None
        event.locked_at = None
    event.last_error = error[:4000]
    await session.flush()
    return event.status


@dataclass
class RunStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead_lettered: int = 0


async def process_batch(
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    sender: Sender,
    settings: Settings,
) -> RunStats:
    """Claim one batch and deliver every event in its own transaction."""
    stats = RunStats()
    async with session_factory() as claim_session, claim_session.begin():
        events = await claim_due(claim_session, worker_id)
        # Claim is committed: events stay 'processing' with this worker id.
    stats.claimed = len(events)

    for event in events:
        async with session_factory() as session, session.begin():
            # Relock the event so bookkeeping is serialized per row.
            locked = await session.get(OutboxEvent, event.id, with_for_update=True)
            try:
                await sender(locked)
            except Exception as exc:  # delivery failure -> retry or DLQ
                outcome = await mark_failure(
                    session,
                    locked,
                    f"{type(exc).__name__}: {exc}",
                    base_delay=settings.outbox_base_delay,
                    max_delay=settings.outbox_max_delay,
                )
                if outcome == "dead_letter":
                    stats.dead_lettered += 1
                else:
                    stats.retried += 1
                logger.warning(
                    "outbox delivery failed",
                    extra={
                        "extra_fields": {
                            "event_id": str(locked.id),
                            "attempts": locked.attempts,
                            "status": outcome,
                            "error": locked.last_error,
                        }
                    },
                )
                continue
            await mark_sent(session, locked)
            stats.sent += 1
    return stats


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    sender: Sender = default_sender,
    worker_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll loop used by ``python -m app.worker``."""
    wid = worker_id or worker_identity()
    logger.info("outbox worker started", extra={"extra_fields": {"worker_id": wid}})
    while stop_event is None or not stop_event.is_set():
        try:
            stats = await process_batch(session_factory, wid, sender, settings)
            if stats.claimed:
                logger.info(
                    "outbox batch complete",
                    extra={"extra_fields": {"worker_id": wid, **stats.__dict__}},
                )
        except Exception:
            logger.exception("outbox batch error")

        if stop_event is None:
            await asyncio.sleep(settings.worker_poll_interval)
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.worker_poll_interval
            )
    logger.info("outbox worker stopped", extra={"extra_fields": {"worker_id": wid}})
