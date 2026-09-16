import asyncio

from helpers import SIMPLE_RULE, T0, create_policy, publish, put_draft


async def _policy_with_draft(client):
    pid = (await create_policy(client)).json()["id"]
    resp = await put_draft(client, pid, SIMPLE_RULE)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    return pid


async def test_draft_update_requires_if_match(client):
    pid = await _policy_with_draft(client)
    resp = await put_draft(client, pid, SIMPLE_RULE)
    assert resp.status_code == 428
    assert resp.json()["error"]["code"] == "PRECONDITION_REQUIRED"


async def test_draft_update_wrong_revision_returns_412(client):
    pid = await _policy_with_draft(client)
    resp = await put_draft(client, pid, SIMPLE_RULE, revision=99)
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "REVISION_MISMATCH"


async def test_draft_update_success_bumps_revision(client):
    pid = await _policy_with_draft(client)
    resp = await put_draft(client, pid, {"op": "eq", "path": "a", "value": 1}, revision=1)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    assert resp.headers["ETag"] == '"2"'


async def test_concurrent_draft_updates_only_one_wins(client):
    pid = await _policy_with_draft(client)
    r1, r2 = await asyncio.gather(
        put_draft(client, pid, {"op": "eq", "path": "a", "value": 1}, revision=1),
        put_draft(client, pid, {"op": "eq", "path": "a", "value": 2}, revision=1),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 412]
    draft = await client.get(f"/policies/{pid}/draft", headers={"X-Tenant-ID": "tenant-a"})
    assert draft.json()["revision"] == 2


async def test_publish_with_stale_revision_returns_412(client):
    pid = await _policy_with_draft(client)
    resp = await publish(client, pid, T0, revision=7)
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "REVISION_MISMATCH"


async def test_publish_without_if_match_returns_428(client):
    pid = await _policy_with_draft(client)
    resp = await publish(client, pid, T0)
    assert resp.status_code == 428


async def test_publish_bumps_draft_revision(client):
    pid = await _policy_with_draft(client)
    resp = await publish(client, pid, T0, revision=1)
    assert resp.status_code == 201
    draft = await client.get(f"/policies/{pid}/draft", headers={"X-Tenant-ID": "tenant-a"})
    assert draft.json()["revision"] == 2


async def test_published_version_is_immutable(client):
    pid = await _policy_with_draft(client)
    version = (await publish(client, pid, T0, revision=1)).json()
    vid = version["id"]
    headers = {"X-Tenant-ID": "tenant-a", "If-Match": '"1"'}
    for method in ("put", "patch", "delete"):
        resp = await client.request(
            method, f"/policies/{pid}/versions/{vid}", headers=headers, json={}
        )
        # No mutating route exists for published versions: 404 (unknown path)
        # or 405 (known path, wrong method) — never a successful write.
        assert resp.status_code in (404, 405), method
    # The version is still there, unchanged.
    versions = await client.get(f"/policies/{pid}/versions", headers=headers)
    assert [v["id"] for v in versions.json()] == [vid]
