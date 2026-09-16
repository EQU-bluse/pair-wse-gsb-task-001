import asyncio
from datetime import UTC, datetime

from helpers import SIMPLE_RULE, T0, create_policy, publish, put_draft
from sqlalchemy import func, select

from app.config import Settings
from app.db import SessionLocal
from app.models import OutboxEvent
from app.worker import process_pending_events, run_worker


async def _outbox_events():
    async with SessionLocal() as s:
        return list((await s.execute(select(OutboxEvent))).scalars().all())


async def _make_events(n: int):
    async with SessionLocal() as s:
        for _ in range(n):
            s.add(OutboxEvent(tenant_id="tenant-a", event_type="policy.published", payload={}))
        await s.commit()


async def test_publish_writes_outbox_event_in_same_transaction(client):
    pid = (await create_policy(client, name="outbox")).json()["id"]
    await put_draft(client, pid, SIMPLE_RULE)
    version = (await publish(client, pid, T0, revision=1)).json()

    events = await _outbox_events()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "policy.published"
    assert event.status == "pending"
    assert event.attempts == 0
    assert event.payload["policy_id"] == pid
    assert event.payload["version_id"] == version["id"]


async def test_worker_once_marks_events_sent(client):
    pid = (await create_policy(client, name="once")).json()["id"]
    await put_draft(client, pid, SIMPLE_RULE)
    await publish(client, pid, T0, revision=1)

    await run_worker(once=True)  # exercises the --once code path

    events = await _outbox_events()
    assert [e.status for e in events] == ["sent"]
    assert events[0].processed_at is not None


async def test_failed_dispatch_retries_with_backoff():
    await _make_events(1)
    settings = Settings(worker_max_retries=5, worker_backoff_base_seconds=10.0)

    def failing_dispatch(event):
        raise RuntimeError("broker down")

    async with SessionLocal() as s:
        assert await process_pending_events(s, settings, dispatch=failing_dispatch) == 1

    events = await _outbox_events()
    assert events[0].status == "pending"
    assert events[0].attempts == 1
    assert events[0].next_attempt_at > datetime.now(UTC)


async def test_event_goes_dead_after_max_retries():
    await _make_events(1)
    settings = Settings(worker_max_retries=2, worker_backoff_base_seconds=0.0)

    def failing_dispatch(event):
        raise RuntimeError("still down")

    for expected_attempts in (1, 2):
        async with SessionLocal() as s:
            await process_pending_events(s, settings, dispatch=failing_dispatch)
        events = await _outbox_events()
        assert events[0].attempts == expected_attempts

    assert events[0].status == "dead"

    # Dead events are never claimed again.
    async with SessionLocal() as s:
        assert await process_pending_events(s, settings) == 0


async def test_concurrent_workers_never_double_claim():
    await _make_events(20)
    settings = Settings(worker_batch_size=10)
    dispatched: list[str] = []

    def recording_dispatch(event):
        dispatched.append(str(event.id))

    async with SessionLocal() as s1, SessionLocal() as s2:
        n1, n2 = await asyncio.gather(
            process_pending_events(s1, settings, dispatch=recording_dispatch),
            process_pending_events(s2, settings, dispatch=recording_dispatch),
        )

    assert n1 + n2 == 20
    assert len(dispatched) == 20
    assert len(set(dispatched)) == 20  # no event processed twice

    async with SessionLocal() as s:
        sent = (
            await s.execute(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "sent")
            )
        ).scalar_one()
    assert sent == 20
