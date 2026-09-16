"""Declarative rule engine.

Rules are plain JSON trees — never evaluated with ``eval``/``exec``. Every node
is interpreted by this module and evaluation produces a deterministic
:class:`TraceNode` recording the operator, resolved input and result.
"""

from __future__ import annotations

from typing import Any

from app.errors import UnprocessableRule
from app.schemas import TraceNode

_LOGIC_OPS = {"all", "any", "not"}
_COMPARISON_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}
_ALLOWED_KEYS = {"op", "path", "value", "values", "rule", "rules"}


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<MISSING>"


MISSING: Any = _Missing()


def _equal(a: Any, b: Any) -> bool:
    # Strict type equality (1 != True), except ints and floats compare by value.
    if _is_real_number(a) and _is_real_number(b):
        return a == b
    return type(a) is type(b) and a == b


def _is_real_number(v: Any) -> bool:
    # bool is a subclass of int in Python; JSON booleans are not numbers.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _orderable(a: Any, b: Any) -> bool:
    if _is_real_number(a) and _is_real_number(b):
        return True
    return isinstance(a, str) and isinstance(b, str)


def resolve_path(context: Any, path: str) -> Any:
    """Resolve a dot-separated path against a JSON-like context.

    Returns :data:`MISSING` when any segment is absent.
    """
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def validate_rule(node: Any) -> None:
    """Recursively validate a rule tree, raising :class:`UnprocessableRule`."""
    if not isinstance(node, dict):
        raise UnprocessableRule("rule node must be an object")
    extra = set(node) - _ALLOWED_KEYS
    if extra:
        raise UnprocessableRule(f"unexpected rule fields: {sorted(extra)}")
    op = node.get("op")
    if not isinstance(op, str):
        raise UnprocessableRule("rule node requires string 'op'")

    if op in _LOGIC_OPS:
        if op == "not":
            child = node.get("rule")
            if not isinstance(child, dict):
                raise UnprocessableRule("'not' node requires an object 'rule'")
            validate_rule(child)
        else:
            children = node.get("rules")
            if not isinstance(children, list) or not children:
                raise UnprocessableRule(f"'{op}' node requires non-empty 'rules' list")
            for child in children:
                validate_rule(child)
        return

    if op in _COMPARISON_OPS:
        if not isinstance(node.get("path"), str) or not node["path"]:
            raise UnprocessableRule(f"'{op}' node requires non-empty string 'path'")
        if "value" not in node:
            raise UnprocessableRule(f"'{op}' node requires 'value'")
        return

    if op == "in":
        if not isinstance(node.get("path"), str) or not node["path"]:
            raise UnprocessableRule("'in' node requires non-empty string 'path'")
        values = node.get("values")
        if not isinstance(values, list):
            raise UnprocessableRule("'in' node requires a 'values' list")
        return

    if op == "exists":
        if not isinstance(node.get("path"), str) or not node["path"]:
            raise UnprocessableRule("'exists' node requires non-empty string 'path'")
        return

    raise UnprocessableRule(f"unknown operator: {op}")


def _trace_comparison(node: dict[str, Any], context: Any) -> TraceNode:
    op = node["op"]
    path = node["path"]
    resolved = resolve_path(context, path)

    if op == "exists":
        found = resolved is not MISSING
        return TraceNode(
            op=op,
            path=path,
            input=None if resolved is MISSING else resolved,
            result=found,
            reason=None if found else "path not found",
        )

    comparand = node.get("value")
    if resolved is MISSING:
        # Missing input never satisfies an ordering/equality check; ``ne`` is
        # the literal negation of ``eq``, so it matches missing input.
        result = op == "ne"
        return TraceNode(
            op=op,
            path=path,
            input=None,
            value=comparand,
            result=result,
            reason="path not found" if op != "ne" else "path not found; ne matches",
        )

    if op in ("eq", "ne"):
        equal = _equal(resolved, comparand)
        result = equal if op == "eq" else not equal
    else:
        if not _orderable(resolved, comparand):
            return TraceNode(
                op=op,
                path=path,
                input=resolved,
                value=comparand,
                result=False,
                reason="incomparable types",
            )
        result = {
            "gt": resolved > comparand,
            "gte": resolved >= comparand,
            "lt": resolved < comparand,
            "lte": resolved <= comparand,
        }[op]

    return TraceNode(op=op, path=path, input=resolved, value=comparand, result=result)


def _trace_in(node: dict[str, Any], context: Any) -> TraceNode:
    path = node["path"]
    resolved = resolve_path(context, path)
    values = node["values"]
    if resolved is MISSING:
        return TraceNode(
            op="in",
            path=path,
            input=None,
            value=values,
            result=False,
            reason="path not found",
        )
    matched = any(_equal(resolved, candidate) for candidate in values)
    return TraceNode(op="in", path=path, input=resolved, value=values, result=matched)


def evaluate(node: dict[str, Any], context: Any) -> TraceNode:
    """Evaluate a validated rule tree against ``context`` and return a trace."""
    op = node["op"]

    if op == "all":
        children = [evaluate(child, context) for child in node["rules"]]
        return TraceNode(op=op, result=all(c.result for c in children), children=children)
    if op == "any":
        children = [evaluate(child, context) for child in node["rules"]]
        return TraceNode(op=op, result=any(c.result for c in children), children=children)
    if op == "not":
        child = evaluate(node["rule"], context)
        return TraceNode(op=op, result=not child.result, children=[child])
    if op == "in":
        return _trace_in(node, context)
    return _trace_comparison(node, context)
