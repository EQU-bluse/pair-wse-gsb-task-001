"""Rule engine: every operator, nesting, dotted paths and deterministic trace."""

from __future__ import annotations

import pytest

from app.errors import UnprocessableRule
from app.rules.engine import evaluate, resolve_path, validate_rule
from tests.helpers import create_draft, create_policy, decide, publish_version


def test_resolve_dotted_and_missing():
    ctx = {"device": {"temperature": 42, "tags": ["a", "b"]}}
    assert resolve_path(ctx, "device.temperature") == 42
    assert resolve_path(ctx, "device.tags.1") == "b"
    assert resolve_path(ctx, "device.missing").__class__.__name__ == "_Missing"


@pytest.mark.parametrize(
    "op,value,context,expected",
    [
        ("eq", 10, {"x": 10}, True),
        ("eq", 10, {"x": 11}, False),
        ("ne", 10, {"x": 11}, True),
        ("gt", 10, {"x": 11}, True),
        ("gte", 10, {"x": 10}, True),
        ("lt", 10, {"x": 9}, True),
        ("lte", 10, {"x": 10}, True),
        ("in", None, {"x": "b"}, True),
        ("exists", None, {"x": 1}, True),
    ],
)
def test_operators(op, value, context, expected):
    if op == "in":
        node = {"op": "in", "path": "x", "values": ["a", "b", "c"]}
    elif op == "exists":
        node = {"op": "exists", "path": "x"}
    else:
        node = {"op": op, "path": "x", "value": value}
    assert evaluate(node, context).result is expected


def test_eq_strict_types_and_in_membership():
    assert evaluate({"op": "eq", "path": "x", "value": 1}, {"x": True}).result is False
    assert evaluate({"op": "eq", "path": "x", "value": 1}, {"x": 1.0}).result is True
    assert evaluate({"op": "in", "path": "x", "values": [1, 2]}, {"x": 5}).result is False
    assert evaluate({"op": "in", "path": "x", "values": [1, 2]}, {"x": 2}).result is True


def test_nested_not_any_all_trace():
    rule = {
        "op": "all",
        "rules": [
            {"op": "gt", "path": "device.temperature", "value": 30},
            {
                "op": "not",
                "rule": {"op": "eq", "path": "device.mode", "value": "off"},
            },
            {
                "op": "any",
                "rules": [
                    {"op": "in", "path": "device.region", "values": ["eu", "us"]},
                    {"op": "exists", "path": "device.override"},
                ],
            },
        ],
    }
    validate_rule(rule)
    ctx = {"device": {"temperature": 42, "mode": "cool", "region": "ap"}}
    trace = evaluate(rule, ctx)
    assert trace.op == "all"
    assert trace.result is False  # the any(...) branch fails
    temp_node = trace.children[0]
    assert temp_node.op == "gt"
    assert temp_node.input == 42 and temp_node.value == 30 and temp_node.result is True
    not_node = trace.children[1]
    assert not_node.op == "not" and not_node.result is True
    any_node = trace.children[2]
    assert any_node.op == "any" and any_node.result is False
    assert [c.op for c in any_node.children] == ["in", "exists"]
    assert any_node.children[1].reason == "path not found"


def test_invalid_rules_rejected():
    bad = [
        {"op": "eval", "path": "x"},
        {"op": "all"},
        {"op": "all", "rules": []},
        {"op": "not", "rule": {}},
        {"op": "gt", "path": "x"},
        {"op": "in", "path": "x"},
        {"op": "eq", "path": "x", "value": 1, "evil": True},
        "not-a-node",
    ]
    for node in bad:
        with pytest.raises(UnprocessableRule):
            validate_rule(node)


async def test_decision_returns_full_trace(client):
    rule = {
        "op": "any",
        "rules": [
            {"op": "gt", "path": "device.temperature", "value": 100},
            {"op": "eq", "path": "device.zone", "value": "restricted"},
        ],
    }
    pid = await create_policy(client, "t", rule)
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", None, 1)

    out = await decide(
        client, "t", pid,
        {"context": {"device": {"temperature": 120, "zone": "normal"}}},
    )
    assert out["result"] is True
    assert out["matched_version"] == 1
    trace = out["trace"]
    assert trace["op"] == "any" and trace["result"] is True
    assert trace["children"][0]["input"] == 120
    assert trace["children"][0]["result"] is True
    assert trace["children"][1]["input"] == "normal"
    assert trace["children"][1]["result"] is False


async def test_draft_change_creates_new_version_rule_after_republish(client):
    pid = await create_policy(client, "t", {"op": "exists", "path": "a"})
    await publish_version(client, "t", pid, "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z", 1)
    await create_draft(client, "t", pid, {"op": "exists", "path": "b"}, 2)
    await publish_version(client, "t", pid, "2025-02-01T00:00:00Z", None, 3)

    jan = await decide(
        client, "t", pid,
        {"context": {"a": 1}, "occurred_at": "2025-01-15T00:00:00Z"},
    )
    feb = await decide(
        client, "t", pid,
        {"context": {"b": 1}, "occurred_at": "2025-02-15T00:00:00Z"},
    )
    assert jan["matched_version"] == 1 and jan["result"] is True
    assert feb["matched_version"] == 2 and feb["result"] is True
