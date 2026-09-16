"""Pydantic v2 request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Rule tree
# ---------------------------------------------------------------------------

ComparisonOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "exists"]
LogicOp = Literal["all", "any", "not"]


class RuleNode(BaseModel):
    """Recursive policy rule node.

    Logic nodes:
      * ``{"op": "all", "rules": [...]}``  — every child must be true
      * ``{"op": "any", "rules": [...]}``  — at least one child must be true
      * ``{"op": "not", "rule": {...}}``   — negates the single child

    Comparison nodes:
      * ``{"op": "eq"|"ne"|"gt"|"gte"|"lt"|"lte", "path": "a.b.c",
          "value": <json>}``
      * ``{"op": "in", "path": "a.b", "values": [...]}``
      * ``{"op": "exists", "path": "a.b"}``
    """

    model_config = ConfigDict(extra="forbid")

    op: str
    path: str | None = None
    value: Any | None = None
    values: list[Any] | None = None
    rule: dict[str, Any] | None = None
    rules: list[dict[str, Any]] | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str | None) -> str | None:
        if v is not None:
            if not v or v.startswith(".") or v.endswith(".") or ".." in v:
                raise ValueError("path must be a non-empty dot-separated identifier path")
            for segment in v.split("."):
                if not segment:
                    raise ValueError("path segments must not be empty")
        return v

    @field_validator("rules")
    @classmethod
    def _validate_rules(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if v is not None and not v:
            raise ValueError("rules must contain at least one child")
        return v


# ---------------------------------------------------------------------------
# Policy / draft
# ---------------------------------------------------------------------------


class PolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    rule: dict[str, Any] | None = None


class PolicyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: str
    name: str
    draft_revision: int
    created_at: datetime
    updated_at: datetime


class DraftView(BaseModel):
    policy_id: uuid.UUID
    revision: int
    rule: dict[str, Any]
    updated_at: datetime


class DraftUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: dict[str, Any]


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_start: datetime
    effective_end: datetime | None = None

    @field_validator("effective_end")
    @classmethod
    def _check_interval(cls, v: datetime | None, info) -> datetime | None:
        start = info.data.get("effective_start")
        if v is not None and start is not None and v <= start:
            raise ValueError("effective_end must be greater than effective_start")
        return v


class VersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    policy_id: uuid.UUID
    tenant_id: str
    version: int
    rule: dict[str, Any]
    effective_start: datetime
    effective_end: datetime | None
    recorded_at: datetime
    revision: int
    created_at: datetime


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    known_at: datetime | None = None


class TraceNode(BaseModel):
    op: str
    path: str | None = None
    input: Any | None = None
    value: Any | None = None
    result: bool
    children: list[TraceNode] | None = None
    reason: str | None = None


TraceNode.model_rebuild()


class DecisionResponse(BaseModel):
    policy_id: uuid.UUID
    result: bool
    matched_version: int | None
    occurred_at: datetime
    known_at: datetime | None
    trace: TraceNode


# ---------------------------------------------------------------------------
# Outbox / health
# ---------------------------------------------------------------------------


class OutboxEventView(BaseModel):
    id: uuid.UUID
    event_type: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    status: str
    attempts: int
    available_at: datetime
    locked_by: str | None
    last_error: str | None
    sent_at: datetime | None
    created_at: datetime


class HealthResponse(BaseModel):
    status: Literal["ok"]
