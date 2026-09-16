import asyncio
from datetime import UTC, datetime

from helpers import SIMPLE_RULE, T0, T1, T2, T3, create_policy, decide, publish, put_draft


async def _policy_with_draft(client):
    pid = (await create_policy(client)).json()["id"]
    assert (await put_draft(client, pid, SIMPLE_RULE)).status_code == 200
    return pid


async def test_half_open_interval_boundaries(client):
    pid = await _policy_with_draft(client)
    v1 = (await publish(client, pid, T0, T1, revision=1)).json()
    v2 = (await publish(client, pid, T1, T2, revision=2)).json()

    # Left-closed: valid_from belongs to the version.
    r = await decide(client, pid, {"device": {"temperature": 25}}, occurred_at=T0)
    assert r.json()["version_id"] == v1["id"]

    # Right-open: valid_to does not; the next version takes over.
    r = await decide(client, pid, {"device": {"temperature": 25}}, occurred_at=T1)
    assert r.json()["version_id"] == v2["id"]

    # Just before T1 still belongs to v1.
    r = await decide(
        client,
        pid,
        {"device": {"temperature": 25}},
        occurred_at=datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC),
    )
    assert r.json()["version_id"] == v1["id"]

    # Before any version: no decision possible.
    r = await decide(client, pid, {}, occurred_at=datetime(2025, 1, 1, tzinfo=UTC))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VERSION_NOT_FOUND"


async def test_open_ended_interval(client):
    pid = await _policy_with_draft(client)
    v = (await publish(client, pid, T0, None, revision=1)).json()
    assert v["valid_to"] is None

    r = await decide(
        client,
        pid,
        {"device": {"temperature": 30}},
        occurred_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    assert r.status_code == 200
    assert r.json()["version_id"] == v["id"]
    assert r.json()["result"] is True


async def test_overlapping_publish_rejected(client):
    pid = await _policy_with_draft(client)
    assert (await publish(client, pid, T0, T2, revision=1)).status_code == 201
    resp = await publish(client, pid, T1, T3, revision=2)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERSION_OVERLAP"


async def test_overlap_with_open_ended_version_rejected(client):
    pid = await _policy_with_draft(client)
    assert (await publish(client, pid, T1, None, revision=1)).status_code == 201
    resp = await publish(client, pid, T2, T3, revision=2)
    assert resp.status_code == 409


async def test_adjacent_intervals_allowed(client):
    pid = await _policy_with_draft(client)
    assert (await publish(client, pid, T0, T1, revision=1)).status_code == 201
    assert (await publish(client, pid, T1, T2, revision=2)).status_code == 201
    versions = await client.get(f"/policies/{pid}/versions", headers={"X-Tenant-ID": "tenant-a"})
    assert len(versions.json()) == 2


async def test_invalid_interval_rejected(client):
    pid = await _policy_with_draft(client)
    resp = await publish(client, pid, T1, T0, revision=1)  # valid_to < valid_from
    assert resp.status_code == 422


async def test_known_at_time_travel(client):
    pid = await _policy_with_draft(client)
    v1 = (await publish(client, pid, T0, T1, revision=1)).json()
    known_at = datetime.fromisoformat(v1["recorded_at"])
    v2 = (await publish(client, pid, T1, None, revision=2)).json()

    # Without known_at, occurred_at=T1 resolves to v2.
    r = await decide(client, pid, {"device": {"temperature": 25}}, occurred_at=T1)
    assert r.json()["version_id"] == v2["id"]

    # With known_at before v2 was recorded, the system "sees" only v1:
    # occurred_at=T1 is then not covered by any visible version.
    r = await decide(
        client, pid, {"device": {"temperature": 25}}, occurred_at=T1, known_at=known_at
    )
    assert r.status_code == 404

    # occurred_at inside v1's interval still resolves to v1 at that known_at.
    r = await decide(
        client, pid, {"device": {"temperature": 25}}, occurred_at=T0, known_at=known_at
    )
    assert r.json()["version_id"] == v1["id"]

    # Version history is also filtered by known_at.
    r = await client.get(
        f"/policies/{pid}/versions",
        params={"known_at": known_at.isoformat()},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert [v["id"] for v in r.json()] == [v1["id"]]


async def test_concurrent_publish_only_one_wins(client):
    pid = await _policy_with_draft(client)
    r1, r2 = await asyncio.gather(
        publish(client, pid, T0, T1, revision=1),
        publish(client, pid, T0, T1, revision=1),
    )
    assert sorted([r1.status_code, r2.status_code]) == [201, 412]
    versions = await client.get(f"/policies/{pid}/versions", headers={"X-Tenant-ID": "tenant-a"})
    assert len(versions.json()) == 1
