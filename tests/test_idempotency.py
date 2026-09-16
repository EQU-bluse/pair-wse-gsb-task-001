import asyncio

from helpers import SIMPLE_RULE, T0, create_policy, publish, put_draft
from sqlalchemy import func, select


async def _policy_count():
    from app.db import SessionLocal
    from app.models import Policy

    async with SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(Policy))).scalar_one()


async def test_create_replay_returns_same_response(client):
    r1 = await create_policy(client, name="idem", key="key-1")
    assert r1.status_code == 201
    r2 = await create_policy(client, name="idem", key="key-1")
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert await _policy_count() == 1


async def test_same_key_different_body_conflicts(client):
    assert (await create_policy(client, name="a", key="key-2")).status_code == 201
    resp = await create_policy(client, name="b", key="key-2")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


async def test_same_key_different_path_is_independent(client):
    # Keys are scoped to (tenant, path): a publish may reuse a key used on create.
    pid = (await create_policy(client, name="scoped", key="key-3")).json()["id"]
    await put_draft(client, pid, SIMPLE_RULE)
    resp = await publish(client, pid, T0, revision=1, key="key-3")
    assert resp.status_code == 201


async def test_concurrent_replays_create_single_resource(client):
    results = await asyncio.gather(
        *[create_policy(client, name="race", key="key-4") for _ in range(5)]
    )
    assert all(r.status_code == 201 for r in results)
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1
    assert await _policy_count() == 1


async def test_publish_replay_returns_same_version(client):
    pid = (await create_policy(client, name="pub-idem")).json()["id"]
    await put_draft(client, pid, SIMPLE_RULE)
    r1 = await publish(client, pid, T0, revision=1, key="pub-key")
    assert r1.status_code == 201
    r2 = await publish(client, pid, T0, revision=1, key="pub-key")
    assert r2.status_code == 201
    assert r2.json() == r1.json()

    versions = await client.get(f"/policies/{pid}/versions", headers={"X-Tenant-ID": "tenant-a"})
    assert len(versions.json()) == 1


async def test_key_scoped_per_tenant(client):
    r1 = await create_policy(client, name="t-a", tenant="tenant-a", key="shared")
    r2 = await create_policy(client, name="t-b", tenant="tenant-b", key="shared")
    assert r1.status_code == 201
    assert r2.status_code == 201
