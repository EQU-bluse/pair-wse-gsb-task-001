"""Tenant isolation: every business endpoint requires and honors X-Tenant-ID."""

from __future__ import annotations

import pytest

from tests.helpers import create_policy, publish_version

pytestmark = pytest.mark.asyncio

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


async def test_missing_tenant_header_rejected(client):
    resp = await client.get("/api/v1/policies/123")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_tenant"


async def test_cross_tenant_policy_is_404(client):
    policy_id = await create_policy(client, TENANT_A, {"op": "exists", "path": "x"})

    resp = await client.get(
        f"/api/v1/policies/{policy_id}", headers={"X-Tenant-ID": TENANT_B}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert TENANT_A not in str(body)


async def test_cross_tenant_versions_and_decide_are_404(client):
    pid = await create_policy(
        client,
        TENANT_A,
        {"op": "all", "rules": [{"op": "exists", "path": "x"}]},
    )
    await publish_version(client, TENANT_A, pid, "2025-01-01T00:00:00Z", None, if_match=1)

    headers_b = {"X-Tenant-ID": TENANT_B}
    versions_resp = await client.get(
        f"/api/v1/policies/{pid}/versions", headers=headers_b
    )
    assert versions_resp.status_code == 404
    decide = await client.post(
        f"/api/v1/policies/{pid}/decide",
        headers=headers_b,
        json={"context": {"x": 1}},
    )
    assert decide.status_code == 404
    draft = await client.get(f"/api/v1/policies/{pid}/draft", headers=headers_b)
    assert draft.status_code == 404


async def test_data_is_logically_partitioned_per_tenant(client):
    # Same policy name in both tenants: both exist independently.
    rule = {"op": "eq", "path": "x", "value": 1}
    id_a = await create_policy(client, TENANT_A, rule, name="dup")
    id_b = await create_policy(client, TENANT_B, rule, name="dup")
    assert id_a != id_b

    list_a = await client.get(
        f"/api/v1/policies/{id_a}", headers={"X-Tenant-ID": TENANT_A}
    )
    list_b = await client.get(
        f"/api/v1/policies/{id_b}", headers={"X-Tenant-ID": TENANT_B}
    )
    assert list_a.status_code == list_b.status_code == 200
