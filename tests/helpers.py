"""Shared HTTP helpers for tests."""

from __future__ import annotations

import uuid
from typing import Any


def h(tenant: str, **extra: str) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant}
    headers.update(extra)
    return headers


def rule_eq(path: str, value: Any) -> dict[str, Any]:
    return {"op": "eq", "path": path, "value": value}


async def create_policy(
    client,
    tenant: str,
    rule: dict[str, Any] | None,
    *,
    name: str = "p",
    idempotency_key: str | None = None,
    expected: int = 201,
) -> str | dict:
    headers = h(tenant)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    resp = await client.post(
        "/api/v1/policies",
        headers=headers,
        json={"name": name, "rule": rule},
    )
    assert resp.status_code == expected, resp.text
    if expected >= 400:
        return resp.json()
    return str(resp.json()["id"])


async def create_draft(
    client, tenant: str, policy_id: str, rule: dict, if_match: int, expected: int = 201
):
    resp = await client.post(
        f"/api/v1/policies/{policy_id}/draft",
        headers=h(tenant, **{"If-Match": str(if_match)}),
        json={"rule": rule},
    )
    assert resp.status_code == expected, resp.text
    return resp


async def update_draft(
    client,
    tenant: str,
    policy_id: str,
    rule: dict,
    if_match: int | None,
    expected: int = 200,
):
    headers = h(tenant)
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    resp = await client.put(
        f"/api/v1/policies/{policy_id}/draft",
        headers=headers,
        json={"rule": rule},
    )
    assert resp.status_code == expected, resp.text
    return resp


async def publish_version(
    client,
    tenant: str,
    policy_id: str,
    start: str,
    end: str | None,
    if_match: int,
    *,
    idempotency_key: str | None = None,
    expected: int = 201,
):
    headers = h(tenant, **{"If-Match": str(if_match)})
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    body: dict[str, Any] = {"effective_start": start}
    if end is not None:
        body["effective_end"] = end
    resp = await client.post(
        f"/api/v1/policies/{policy_id}/publish",
        headers=headers,
        json=body,
    )
    assert resp.status_code == expected, resp.text
    return resp


async def decide(client, tenant: str, policy_id: str, payload: dict, expected: int = 200):
    resp = await client.post(
        f"/api/v1/policies/{policy_id}/decide",
        headers=h(tenant),
        json=payload,
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def unique_key() -> str:
    return f"key-{uuid.uuid4().hex}"
