"""Outbox worker: dispatches persisted events with retries and dead-lettering.

Run with: python -m app.worker [--once]

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED so multiple worker instances
never process the same event. The actual "send" is a structured log line, but
event state transitions are fully persisted in the database.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.logging import configure_logging, get_logger
from app.models import OutboxEvent

logger = get_logger("worker")


def dispatch_event(event: OutboxEvent) -> None:
    """Deliver the event. Real delivery is replaced by a structured log line."""
    logger.info(
        "outbox_event_dispatched",
        event_id=str(event.id),
        event_type=event.event_type,
        tenant_id=event.tenant_id,
        payload=event.payload,
    )


async def claim_pending_events(session: AsyncSession, settings: Settings) -> list[OutboxEvent]:
    # Compare against the database clock so app/db clock skew cannot hide events.
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending", OutboxEvent.next_attempt_at <= func.now())
        .order_by(OutboxEvent.created_at)
        .limit(settings.worker_batch_size)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def process_pending_events(
    session: AsyncSession,
    settings: Settings | None = None,
    dispatch: Any = None,
) -> int:
    """Claim and process one batch. Returns the number of events processed."""
    settings = settings or get_settings()
    dispatch = dispatch if dispatch is not None else dispatch_event

    events = await claim_pending_events(session, settings)
    for event in events:
        try:
            dispatch(event)
        except Exception as exc:
            event.attempts += 1
            if event.attempts >= settings.worker_max_retries:
                event.status = "dead"
                logger.error(
                    "outbox_event_dead",
                    event_id=str(event.id),
                    attempts=event.attempts,
                    error=str(exc),
                )
            else:
                backoff = settings.worker_backoff_base_seconds * (2 ** (event.attempts - 1))
                event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
                logger.warning(
                    "outbox_event_retry_scheduled",
                    event_id=str(event.id),
                    attempts=event.attempts,
                    backoff_seconds=backoff,
                    error=str(exc),
                )
        else:
            event.status = "sent"
            event.processed_at = datetime.now(UTC)
    await session.commit()
    return len(events)


async def run_worker(once: bool = False) -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as session:
                processed = await process_pending_events(session, settings)
        except Exception:
            logger.exception("worker_iteration_failed")
            processed = 0
        if once:
            return
        if processed == 0:
            await asyncio.sleep(settings.worker_poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Outbox event worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process a single batch and exit (useful for tests and demos)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
