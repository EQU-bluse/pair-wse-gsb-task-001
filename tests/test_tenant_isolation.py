from helpers import SIMPLE_RULE, T0, create_policy, decide, publish, put_draft, tenant_headers


async def test_missing_tenant_header_rejected(client):
    resp = await client.post("/policies", json={"name": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_cross_tenant_access_returns_404(client):
    created = await create_policy(client, name="shared-name", tenant="tenant-a")
    pid = created.json()["id"]
    await put_draft(client, pid, SIMPLE_RULE, tenant="tenant-a")
    await publish(client, pid, T0, tenant="tenant-a", revision=1)

    other = tenant_headers("tenant-b")

    assert (await client.get(f"/policies/{pid}", headers=other)).status_code == 404
    assert (await client.get(f"/policies/{pid}/draft", headers=other)).status_code == 404
    assert (await put_draft(client, pid, SIMPLE_RULE, tenant="tenant-b")).status_code == 404
    assert (await publish(client, pid, T0, tenant="tenant-b", revision=1)).status_code == 404
    assert (await client.get(f"/policies/{pid}/versions", headers=other)).status_code == 404
    assert (await decide(client, pid, {}, tenant="tenant-b")).status_code == 404


async def test_tenants_can_use_same_policy_name(client):
    r1 = await create_policy(client, name="same", tenant="tenant-a")
    r2 = await create_policy(client, name="same", tenant="tenant-b")
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


async def test_duplicate_name_same_tenant_conflicts(client):
    assert (await create_policy(client, name="dup")).status_code == 201
    resp = await create_policy(client, name="dup")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "POLICY_NAME_CONFLICT"
