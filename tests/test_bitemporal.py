"""Bitemporal semantics: half-open intervals, open ends, overlap, known_at."""

from __future__ import annotations

from tests.helpers import create_draft, create_policy, decide, publish_version

RULE_V1 = {"op": "eq", "path": "v", "value": "one"}
RULE_V2 = {"op": "eq", "path": "v", "value": "two"}


async def _publish_two_versions(client):
    pid = await create_policy(client, "t", RULE_V1)
    # v1 effective [2025-01-01, 2025-06-01)
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z", 1)
    # After publish revision=2; start a new draft and publish v2, open-ended
    await create_draft(client, "t", pid, RULE_V2, 2)
    await publish_version(client, "t", pid, "2025-06-01T00:00:00Z", None, 3)
    return pid


async def test_half_open_boundary_switches_version(client):
    pid = await _publish_two_versions(client)

    # Just before boundary -> v1
    before = await decide(
        client, "t", pid,
        {"context": {"v": "one"}, "occurred_at": "2025-05-31T23:59:59Z"},
    )
    assert before["matched_version"] == 1 and before["result"] is True
    # Exactly at start of v2 (left-closed/right-open): v2 wins, v1 no longer matches
    boundary = await decide(
        client, "t", pid,
        {"context": {"v": "two"}, "occurred_at": "2025-06-01T00:00:00Z"},
    )
    assert boundary["matched_version"] == 2 and boundary["result"] is True
    # At the boundary v1 is excluded (end is exclusive)
    boundary_one = await decide(
        client, "t", pid,
        {"context": {"v": "one"}, "occurred_at": "2025-06-01T00:00:00Z"},
    )
    assert boundary_one["matched_version"] == 2 and boundary_one["result"] is False


async def test_open_ended_interval_extends_forward(client):
    pid = await _publish_two_versions(client)
    far_future = await decide(
        client, "t", pid,
        {"context": {"v": "two"}, "occurred_at": "2099-01-01T00:00:00Z"},
    )
    assert far_future["matched_version"] == 2
    assert far_future["result"] is True


async def test_before_any_version_matches_none(client):
    pid = await _publish_two_versions(client)
    out = await decide(
        client, "t", pid,
        {"context": {"v": "one"}, "occurred_at": "2024-12-31T00:00:00Z"},
    )
    assert out["matched_version"] is None
    assert out["result"] is False
    assert out["trace"]["op"] == "no_effective_version"


async def test_overlapping_publish_is_rejected(client):
    pid = await create_policy(client, "t", RULE_V1)
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z", 1)
    await create_draft(client, "t", pid, RULE_V2, 2)
    # Overlaps [2025-01-01, 2025-06-01)
    resp = await publish_version(
        client, "t", pid, "2025-05-01T00:00:00Z", None, 3, expected=409
    )
    assert resp.json()["error"]["code"] == "effective_interval_overlap"

    # Adjacent (touching but not overlapping) interval is allowed.
    resp2 = await publish_version(
        client, "t", pid, "2025-06-01T00:00:00Z", None, 3, expected=201
    )
    assert resp2.json()["version"] == 2


async def test_invalid_interval_rejected(client):
    pid = await create_policy(client, "t", RULE_V1)
    resp = await publish_version(
        client, "t", pid, "2025-06-01T00:00:00Z", "2025-01-01T00:00:00Z", 1, expected=422
    )
    assert resp.json()["error"]["code"] == "validation_error"


async def test_version_history_is_ordered(client):
    pid = await _publish_two_versions(client)
    resp = await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )
    versions = resp.json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["effective_end"] is not None
    assert versions[1]["effective_end"] is None
    for v in versions:
        assert "recorded_at" in v


async def test_known_at_reconstructs_past_system_view(client):
    """A version recorded later must be invisible to an earlier known_at."""
    import asyncio

    pid = await create_policy(client, "t", RULE_V1)
    # v1 effective [2025-01-01, 2025-06-01), recorded at t1.
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z", 1)
    t1 = (await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )).json()[0]["recorded_at"]

    # Ensure v2 gets a strictly later recorded_at (system time axis).
    await asyncio.sleep(0.02)
    await create_draft(client, "t", pid, RULE_V2, 2)
    await publish_version(client, "t", pid, "2025-06-01T00:00:00Z", None, 3)
    versions = (await client.get(
        f"/api/v1/policies/{pid}/versions", headers={"X-Tenant-ID": "t"}
    )).json()
    t2 = versions[1]["recorded_at"]
    assert t2 > t1

    # Business time inside v2's range, but system view frozen at t1:
    # v1 not yet effective there and v2 not yet recorded -> nothing.
    hidden = await decide(
        client, "t", pid,
        {"context": {"v": "two"}, "occurred_at": "2025-07-01T00:00:00Z",
         "known_at": t1},
    )
    assert hidden["matched_version"] is None

    # Same business time with the current system view sees v2.
    visible = await decide(
        client, "t", pid,
        {"context": {"v": "two"}, "occurred_at": "2025-07-01T00:00:00Z",
         "known_at": t2},
    )
    assert visible["matched_version"] == 2

    # Business time inside v1 with known_at in the distant past sees nothing.
    past = await decide(
        client, "t", pid,
        {"context": {"v": "one"}, "occurred_at": "2025-02-01T00:00:00Z",
         "known_at": "2000-01-01T00:00:00Z"},
    )
    assert past["matched_version"] is None
