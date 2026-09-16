from datetime import UTC, datetime

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
T3 = datetime(2026, 4, 1, tzinfo=UTC)

SIMPLE_RULE = {"op": "gte", "path": "device.temperature", "value": 20}


def tenant_headers(tenant: str = "tenant-a") -> dict:
    return {"X-Tenant-ID": tenant}


async def create_policy(client, name="p1", tenant="tenant-a", key=None):
    headers = tenant_headers(tenant)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post("/policies", json={"name": name}, headers=headers)


async def put_draft(client, policy_id, rules, tenant="tenant-a", revision=None):
    headers = tenant_headers(tenant)
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return await client.put(f"/policies/{policy_id}/draft", json={"rules": rules}, headers=headers)


async def publish(
    client, policy_id, valid_from, valid_to=None, tenant="tenant-a", revision=None, key=None
):
    headers = tenant_headers(tenant)
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    if key is not None:
        headers["Idempotency-Key"] = key
    body = {"valid_from": valid_from.isoformat()}
    if valid_to is not None:
        body["valid_to"] = valid_to.isoformat()
    return await client.post(f"/policies/{policy_id}/publish", json=body, headers=headers)


async def decide(client, policy_id, context, tenant="tenant-a", occurred_at=None, known_at=None):
    body = {"context": context}
    if occurred_at is not None:
        body["occurred_at"] = occurred_at.isoformat()
    if known_at is not None:
        body["known_at"] = known_at.isoformat()
    return await client.post(
        f"/policies/{policy_id}/decisions", json=body, headers=tenant_headers(tenant)
    )
