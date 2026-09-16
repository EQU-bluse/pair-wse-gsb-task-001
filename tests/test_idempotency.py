"""Idempotency-Key replay, body-conflict and concurrent-replay semantics."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import IdempotencyKey, Policy
from tests.helpers import create_policy, unique_key


async def test_replay_returns_first_response(client):
    key = unique_key()
    headers = {"X-Tenant-ID": "t", "Idempotency-Key": key}
    payload = {"name": "once", "rule": {"op": "exists", "path": "x"}}

    first = await client.post("/api/v1/policies", headers=headers, json=payload)
    assert first.status_code == 201
    first_body = first.json()

    second = await client.post("/api/v1/policies", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json() == first_body
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert "Idempotency-Replayed" not in first.headers

    # Exactly one policy was created.
    async with SessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(Policy))
        assert count == 1


async def test_same_key_different_body_conflicts(client):
    key = unique_key()
    headers = {"X-Tenant-ID": "t", "Idempotency-Key": key}
    await client.post(
        "/api/v1/policies",
        headers=headers,
        json={"name": "first", "rule": {"op": "exists", "path": "x"}},
    )
    conflict = await client.post(
        "/api/v1/policies",
        headers=headers,
        json={"name": "different", "rule": {"op": "exists", "path": "y"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


async def test_idempotency_scoped_by_tenant_and_path(client):
    key = unique_key()
    payload = {"name": "p", "rule": {"op": "exists", "path": "x"}}
    a = await client.post(
        "/api/v1/policies",
        headers={"X-Tenant-ID": "tenant-a", "Idempotency-Key": key},
        json=payload,
    )
    b = await client.post(
        "/api/v1/policies",
        headers={"X-Tenant-ID": "tenant-b", "Idempotency-Key": key},
        json=payload,
    )
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


async def test_idempotency_deterministic_body_hash_ignores_key_order(client):
    key = unique_key()
    headers = {"X-Tenant-ID": "t", "Idempotency-Key": key}
    first = await client.post(
        "/api/v1/policies",
        headers=headers,
        json={"rule": {"op": "exists", "path": "x"}, "name": "ordered"},
    )
    assert first.status_code == 201
    # Same JSON semantics, different textual key order -> same canonical hash.
    replay = await client.post(
        "/api/v1/policies",
        headers=headers,
        json={"name": "ordered", "rule": {"path": "x", "op": "exists"}},
    )
    assert replay.status_code == 201
    assert replay.headers.get("Idempotency-Replayed") == "true"


async def test_concurrent_replays_create_single_resource(client):
    key = unique_key()
    headers = {"X-Tenant-ID": "t", "Idempotency-Key": key}
    payload = {"name": "race", "rule": {"op": "exists", "path": "x"}}

    responses = await asyncio.gather(
        *[
            client.post("/api/v1/policies", headers=headers, json=payload)
            for _ in range(8)
        ]
    )
    statuses = [r.status_code for r in responses]
    assert statuses == [201] * 8
    bodies = {r.json()["id"] for r in responses}
    assert len(bodies) == 1

    async with SessionLocal() as session:
        policies = await session.scalar(select(func.count()).select_from(Policy))
        markers = await session.scalar(select(func.count()).select_from(IdempotencyKey))
        assert policies == 1
        assert markers == 1


async def test_publish_replay_does_not_create_two_versions(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "x"})
    key = unique_key()
    body = {"effective_start": "2025-03-01T00:00:00Z"}
    headers = {"X-Tenant-ID": "t", "If-Match": "1", "Idempotency-Key": key}

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/policies/{pid}/publish", headers=headers, json=body)
            for _ in range(5)
        ]
    )
    assert [r.status_code for r in results] == [201] * 5
    assert len({r.json()["id"] for r in results}) == 1

    listing = await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )
    assert len(listing.json()) == 1
