"""Draft lifecycle, optimistic concurrency and published-version immutability."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from tests.helpers import create_draft, create_policy, publish_version, update_draft


async def test_policy_created_with_initial_draft_revision_one(client):
    resp = await client.post(
        "/api/v1/policies",
        headers={"X-Tenant-ID": "t"},
        json={"name": "p", "rule": {"op": "exists", "path": "x"}},
    )
    assert resp.status_code == 201
    assert resp.headers["ETag"] == '"1"'
    body = resp.json()
    assert body["draft_revision"] == 1


async def test_policy_without_rule_has_no_draft(client):
    pid = await create_policy(client, "t", None, name="empty")
    resp = await client.get(f"/api/v1/policies/{pid}/draft", headers={"X-Tenant-ID": "t"})
    assert resp.status_code == 404


async def test_draft_update_requires_if_match(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    resp = await client.put(
        f"/api/v1/policies/{pid}/draft",
        headers={"X-Tenant-ID": "t"},
        json={"rule": {"op": "exists", "path": "y"}},
    )
    assert resp.status_code == 428
    assert resp.json()["error"]["code"] == "precondition_required"


async def test_draft_update_stale_revision_returns_412(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    ok = await update_draft(client, "t", pid, {"op": "exists", "path": "y"}, 1)
    assert ok.status_code == 200
    assert ok.headers["ETag"] == '"2"'

    stale = await update_draft(
        client, "t", pid, {"op": "exists", "path": "z"}, 1, expected=412
    )
    assert stale.json()["error"]["details"]["current_revision"] == 2


async def test_if_match_must_be_numeric(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    resp = await update_draft_raw(client, pid, "abc")
    assert resp.status_code == 400


async def update_draft_raw(client, pid, if_match):
    return await client.put(
        f"/api/v1/policies/{pid}/draft",
        headers={"X-Tenant-ID": "t", "If-Match": if_match},
        json={"rule": {"op": "exists", "path": "z"}},
    )


async def test_publish_consumes_draft_and_second_publish_is_404(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    first = await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", None, 1)
    assert first.status_code == 201

    # Draft was consumed; publishing again without a new draft -> 404.
    await publish_version(
        client, "t", pid, "2026-01-01T00:00:00Z", None, 2, expected=404
    )
    # And GET draft reports it is gone.
    resp = await client.get(f"/api/v1/policies/{pid}/draft", headers={"X-Tenant-ID": "t"})
    assert resp.status_code == 404


async def test_create_new_draft_after_publish_requires_current_revision(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", None, 1)

    # Revision is 2 after publish; a stale If-Match: 1 must fail with 412.
    stale = await create_draft(
        client, "t", pid, {"op": "exists", "path": "a"}, 1, expected=412
    )
    assert stale.json()["error"]["details"]["current_revision"] == 2

    ok = await create_draft(client, "t", pid, {"op": "exists", "path": "a"}, 2)
    assert ok.status_code == 201


async def test_published_versions_are_immutable_in_database(client):
    """The database itself rejects UPDATE/DELETE on published versions."""
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", None, 1)

    async with SessionLocal() as session:
        with pytest.raises(Exception) as update_exc:
            async with session.begin():
                await session.execute(
                    text("UPDATE policy_versions SET version = 99 WHERE true")
                )
        assert "immutable" in str(update_exc.value)

    async with SessionLocal() as session:
        with pytest.raises(Exception) as delete_exc:
            async with session.begin():
                await session.execute(text("DELETE FROM policy_versions WHERE true"))
        assert "immutable" in str(delete_exc.value)


async def test_invalid_rule_is_rejected_at_write_time(client):
    resp = await client.post(
        "/api/v1/policies",
        headers={"X-Tenant-ID": "t"},
        json={"name": "p", "rule": {"op": "eval", "path": "x"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_rule"

    resp2 = await client.post(
        "/api/v1/policies",
        headers={"X-Tenant-ID": "t"},
        json={"name": "p", "rule": {"op": "all", "rules": []}},
    )
    assert resp2.status_code == 422
