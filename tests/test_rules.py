import json

from helpers import T0, create_policy, decide, publish, put_draft

from app.rules import evaluate

NESTED_RULE = {
    "op": "all",
    "children": [
        {"op": "gte", "path": "device.temperature", "value": 20},
        {
            "op": "any",
            "children": [
                {"op": "eq", "path": "user.role", "value": "admin"},
                {"op": "in", "path": "user.group", "value": ["ops", "sre"]},
            ],
        },
        {"op": "not", "child": {"op": "eq", "path": "device.maintenance", "value": True}},
        {"op": "exists", "path": "device"},
    ],
}

CONTEXT = {
    "device": {"temperature": 25, "maintenance": False},
    "user": {"role": "viewer", "group": "ops"},
}


def test_evaluate_nested_trace_is_deterministic():
    trace1 = evaluate(NESTED_RULE, CONTEXT)
    trace2 = evaluate(NESTED_RULE, CONTEXT)
    assert trace1 == trace2
    assert json.dumps(trace1, sort_keys=True) == json.dumps(trace2, sort_keys=True)

    assert trace1 == {
        "op": "all",
        "result": True,
        "children": [
            {
                "op": "gte",
                "path": "device.temperature",
                "input": 25,
                "expected": 20,
                "result": True,
            },
            {
                "op": "any",
                "result": True,
                "children": [
                    {
                        "op": "eq",
                        "path": "user.role",
                        "input": "viewer",
                        "expected": "admin",
                        "result": False,
                    },
                    {
                        "op": "in",
                        "path": "user.group",
                        "input": "ops",
                        "expected": ["ops", "sre"],
                        "result": True,
                    },
                ],
            },
            {
                "op": "not",
                "result": True,
                "child": {
                    "op": "eq",
                    "path": "device.maintenance",
                    "input": False,
                    "expected": True,
                    "result": False,
                },
            },
            {
                "op": "exists",
                "path": "device",
                "input": {"temperature": 25, "maintenance": False},
                "result": True,
            },
        ],
    }


def test_missing_path_evaluates_false():
    trace = evaluate({"op": "eq", "path": "a.b.c", "value": 1}, {})
    assert trace == {
        "op": "eq",
        "path": "a.b.c",
        "input": None,
        "input_missing": True,
        "result": False,
    }
    assert evaluate({"op": "exists", "path": "a.b"}, {})["result"] is False


def test_all_comparison_operators():
    ctx = {"x": 10, "s": "b"}
    assert evaluate({"op": "eq", "path": "x", "value": 10}, ctx)["result"] is True
    assert evaluate({"op": "ne", "path": "x", "value": 11}, ctx)["result"] is True
    assert evaluate({"op": "gt", "path": "x", "value": 9}, ctx)["result"] is True
    assert evaluate({"op": "gte", "path": "x", "value": 10}, ctx)["result"] is True
    assert evaluate({"op": "lt", "path": "x", "value": 11}, ctx)["result"] is True
    assert evaluate({"op": "lte", "path": "x", "value": 10}, ctx)["result"] is True
    assert evaluate({"op": "in", "path": "s", "value": ["a", "b"]}, ctx)["result"] is True
    assert evaluate({"op": "gt", "path": "s", "value": 1}, ctx)["result"] is False  # type error


async def test_decision_endpoint_returns_trace(client):
    pid = (await create_policy(client, name="trace")).json()["id"]
    await put_draft(client, pid, NESTED_RULE)
    version = (await publish(client, pid, T0, revision=1)).json()

    resp = await decide(client, pid, CONTEXT, occurred_at=T0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is True
    assert body["version_id"] == version["id"]
    assert body["trace"]["op"] == "all"
    assert body["trace"]["children"][0]["input"] == 25

    # Failing context flips the result and the trace shows why.
    resp = await decide(client, pid, {"device": {"temperature": 5}}, occurred_at=T0)
    body = resp.json()
    assert body["result"] is False
    assert body["trace"]["children"][0]["result"] is False


async def test_invalid_rule_rejected(client):
    pid = (await create_policy(client, name="bad-rule")).json()["id"]
    resp = await put_draft(client, pid, {"op": "eval", "code": "1+1"})
    assert resp.status_code == 422
    resp = await put_draft(client, pid, {"op": "eq", "path": "bad path!", "value": 1})
    assert resp.status_code == 422
