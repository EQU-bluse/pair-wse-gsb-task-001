"""Outbox: transactional event, SKIP LOCKED claiming, retries, dead-letter."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import OutboxEvent
from app.services.outbox import claim_due, default_sender, process_batch, worker_identity
from tests.helpers import create_draft, create_policy, publish_version

SETTINGS = get_settings()


async def _event_count(status: str | None = None) -> int:
    async with SessionLocal() as session:
        stmt = select(func.count()).select_from(OutboxEvent)
        if status:
            stmt = stmt.where(OutboxEvent.status == status)
        return await session.scalar(stmt)


async def test_publish_writes_outbox_event_in_same_transaction(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", None, 1)

    assert await _event_count("pending") == 1
    async with SessionLocal() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "policy.published")
        )
        assert event.tenant_id == "t"
        assert str(event.aggregate_id) == pid
        assert event.payload["version"] == 1
        assert event.payload["policy_id"]
        assert event.max_attempts == SETTINGS.outbox_max_attempts


async def test_failed_publish_writes_no_event(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    await publish_version(
        client, "t", pid, "2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z", 1
    )
    await create_draft(client, "t", pid, {"op": "exists", "path": "y"}, 2)
    # Overlapping publish rolls back: no event, no version.
    await publish_version(
        client, "t", pid, "2025-05-01T00:00:00Z", None, 3, expected=409
    )
    assert await _event_count() == 1  # only the first publish


async def test_worker_process_batch_marks_sent():
    async with SessionLocal() as session, session.begin():
        session.add(
            OutboxEvent(
                event_type="policy.published",
                tenant_id="t",
                aggregate_type="policy",
                aggregate_id=uuid.uuid4(),
                payload={"v": 1},
            )
        )

    stats = await process_batch(SessionLocal, worker_identity(), default_sender, SETTINGS)
    assert stats.claimed == 1 and stats.sent == 1
    assert await _event_count("sent") == 1


async def test_concurrent_workers_claim_disjoint_batches():
    import uuid

    async with SessionLocal() as session, session.begin():
        for i in range(10):
            session.add(
                OutboxEvent(
                    event_type="policy.published",
                    tenant_id="t",
                    aggregate_type="policy",
                    aggregate_id=uuid.uuid4(),
                    payload={"i": i},
                )
            )

    delivered: set[str] = set()
    lock = asyncio.Lock()

    async def recording_sender(event):
        async with lock:
            delivered.add(str(event.id))

    # Two batches run concurrently; SKIP LOCKED guarantees disjoint claims.
    s1, s2 = await asyncio.gather(
        process_batch(SessionLocal, "w1", recording_sender, SETTINGS),
        process_batch(SessionLocal, "w2", recording_sender, SETTINGS),
    )
    assert s1.sent + s2.sent == 10
    assert len(delivered) == 10
    assert await _event_count("sent") == 10


async def test_direct_concurrent_claims_never_double_lock():
    import uuid

    async with SessionLocal() as session, session.begin():
        for _ in range(6):
            session.add(
                OutboxEvent(
                    event_type="policy.published",
                    tenant_id="t",
                    aggregate_type="policy",
                    aggregate_id=uuid.uuid4(),
                    payload={},
                )
            )

    async def claim(wid):
        async with SessionLocal() as session, session.begin():
            events = await claim_due(session, wid, batch_size=10)
            # Hold the locks briefly so both transactions overlap.
            await asyncio.sleep(0.05)
            return {str(e.id) for e in events}

    a, b = await asyncio.gather(claim("wa"), claim("wb"))
    assert a.isdisjoint(b)
    assert len(a) + len(b) == 6


async def test_failing_sender_retries_then_dead_letters():
    import uuid

    async with SessionLocal() as session, session.begin():
        session.add(
            OutboxEvent(
                event_type="policy.published",
                tenant_id="t",
                aggregate_type="policy",
                aggregate_id=uuid.uuid4(),
                payload={},
                max_attempts=3,
            )
        )

    async def boom(event):
        raise RuntimeError("downstream unavailable")

    # Attempt 1 -> pending retry; attempt 2 -> pending retry; attempt 3 -> DLQ.
    for expected_attempts, expected_status in (
        (1, "pending"),
        (2, "pending"),
        (3, "dead_letter"),
    ):
        stats = await process_batch(SessionLocal, worker_identity(), boom, SETTINGS)
        assert stats.claimed == 1
        async with SessionLocal() as session:
            event = await session.scalar(select(OutboxEvent))
            assert event.attempts == expected_attempts
            assert event.status == expected_status
            assert "downstream unavailable" in (event.last_error or "")

    # Dead-lettered events are never claimed again.
    stats = await process_batch(SessionLocal, worker_identity(), default_sender, SETTINGS)
    assert stats.claimed == 0


async def test_stale_processing_event_is_reclaimed():
    """A 'processing' event whose worker died becomes claimable after timeout."""
    import uuid
    from datetime import UTC, datetime
    from datetime import timedelta as td

    event_id = uuid.uuid4()
    async with SessionLocal() as session, session.begin():
        session.add(
            OutboxEvent(
                id=event_id,
                event_type="policy.published",
                tenant_id="t",
                aggregate_type="policy",
                aggregate_id=uuid.uuid4(),
                payload={},
                status="processing",
                attempts=1,
                locked_by="dead-worker",
                locked_at=datetime.now(UTC) - td(hours=1),
            )
        )

    async with SessionLocal() as session, session.begin():
        claimed = await claim_due(session, "w-recovery")
        assert len(claimed) == 1
        assert claimed[0].id == event_id
        assert claimed[0].attempts == 2
        assert claimed[0].locked_by == "w-recovery"


async def test_worker_once_cli():
    import uuid

    async with SessionLocal() as session, session.begin():
        session.add(
            OutboxEvent(
                event_type="policy.published",
                tenant_id="t",
                aggregate_type="policy",
                aggregate_id=uuid.uuid4(),
                payload={"cli": True},
            )
        )

    env = dict(os.environ)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.worker", "--once",
        cwd=root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    assert b"worker --once finished" in out
    assert await _event_count("sent") == 1
