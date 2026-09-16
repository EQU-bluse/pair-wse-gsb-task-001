"""Deterministic, side-effect-free rule evaluation.

Rules are plain JSON documents interpreted by a recursive walker — no eval/exec
or any other form of dynamic code execution. Every evaluation produces a trace
tree mirroring the rule structure with per-node operator, inputs and result.
"""

from typing import Any

COMPARISON_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}


class _Missing:
    pass


MISSING = _Missing()


def resolve_path(context: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path like `device.temperature` against the context."""
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "eq":
        return bool(actual == expected)
    if op == "ne":
        return bool(actual != expected)
    if op == "gt":
        return bool(actual > expected)
    if op == "gte":
        return bool(actual >= expected)
    if op == "lt":
        return bool(actual < expected)
    if op == "lte":
        return bool(actual <= expected)
    if op == "in":
        return bool(actual in expected)
    raise ValueError(f"unsupported operator: {op}")


def evaluate(node: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a rule node, returning a deterministic trace dict.

    Trace keys are inserted in a fixed order so serialized traces are stable.
    """
    op = node["op"]

    if op in ("all", "any"):
        children = [evaluate(child, context) for child in node["children"]]
        results = [child["result"] for child in children]
        result = all(results) if op == "all" else any(results)
        return {"op": op, "result": result, "children": children}

    if op == "not":
        child = evaluate(node["child"], context)
        return {"op": "not", "result": not child["result"], "child": child}

    # Leaf operators: eq / ne / gt / gte / lt / lte / in / exists
    path = node["path"]
    value = resolve_path(context, path)
    trace: dict[str, Any] = {"op": op, "path": path}

    if value is MISSING:
        trace["input"] = None
        trace["input_missing"] = True
        trace["result"] = False
        return trace

    trace["input"] = value

    if op == "exists":
        trace["result"] = True
        return trace

    expected = node.get("value")
    trace["expected"] = expected
    try:
        trace["result"] = _compare(op, value, expected)
    except TypeError:
        # Incomparable types (e.g. `gt` between str and int) evaluate to false.
        trace["result"] = False
        trace["error"] = "incomparable_types"
    return trace
