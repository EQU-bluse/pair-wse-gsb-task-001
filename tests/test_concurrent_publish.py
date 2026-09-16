"""Concurrent publishing: row locks + exclusion constraint guarantee uniqueness."""

from __future__ import annotations

import asyncio

from tests.helpers import create_policy, publish_version


async def test_concurrent_publish_creates_single_version(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    body = {"effective_start": "2025-03-01T00:00:00Z"}
    headers = {"X-Tenant-ID": "t", "If-Match": "1"}

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/policies/{pid}/publish", headers=headers, json=body)
            for _ in range(6)
        ]
    )
    statuses = sorted(r.status_code for r in results)
    # Exactly one publish wins; the rest are serialized behind the policy row
    # lock and find the draft already consumed.
    assert statuses.count(201) == 1
    assert all(s in (404, 412) for s in statuses if s != 201)

    listing = await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )
    assert len(listing.json()) == 1


async def test_concurrent_publish_second_waiter_sees_consumed_draft(client):
    """Two requests race one draft: row lock serializes them; the waiter then
    observes the consumed draft and fails instead of double-publishing."""
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    await publish_version(
        client, "t", pid, "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z", 1
    )
    # revision 2 after publish; create draft -> revision 3.
    draft = await client.post(
        f"/api/v1/policies/{pid}/draft",
        headers={"X-Tenant-ID": "t", "If-Match": "2"},
        json={"rule": {"op": "exists", "path": "y"}},
    )
    assert draft.status_code == 201

    async def fire(start, end):
        return await client.post(
            f"/api/v1/policies/{pid}/publish",
            headers={"X-Tenant-ID": "t", "If-Match": "3"},
            json={"effective_start": start, "effective_end": end},
        )

    r1, r2 = await asyncio.gather(
        fire("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z"),
        fire("2025-03-15T00:00:00Z", "2025-04-15T00:00:00Z"),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [201, 404]

    listing = await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )
    assert len(listing.json()) == 2
