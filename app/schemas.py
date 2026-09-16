import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _validate_path(path: str) -> str:
    if not _PATH_RE.match(path):
        raise ValueError(f"invalid context path: {path!r}")
    return path


class _PathRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return _validate_path(v)


class ComparisonRule(_PathRule):
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: Any


class InRule(_PathRule):
    op: Literal["in"]
    value: list[Any] = Field(min_length=1)


class ExistsRule(_PathRule):
    op: Literal["exists"]


class AllRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["all"]
    children: list["RuleNode"] = Field(min_length=1)


class AnyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["any"]
    children: list["RuleNode"] = Field(min_length=1)


class NotRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["not"]
    child: "RuleNode"


RuleNode = Annotated[
    ComparisonRule | InRule | ExistsRule | AllRule | AnyRule | NotRule,
    Field(discriminator="op"),
]

AllRule.model_rebuild()
AnyRule.model_rebuild()
NotRule.model_rebuild()


def _aware(v: datetime) -> datetime:
    """Naive datetimes are interpreted as UTC."""
    if v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime


class DraftUpdate(BaseModel):
    rules: RuleNode


class DraftResponse(BaseModel):
    policy_id: uuid.UUID
    revision: int
    rules: dict[str, Any]
    updated_at: datetime


class PublishRequest(BaseModel):
    valid_from: datetime
    valid_to: datetime | None = None

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _coerce_tz(cls, v: datetime | None) -> datetime | None:
        return _aware(v) if v is not None else None

    @model_validator(mode="after")
    def _check_range(self) -> "PublishRequest":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be greater than valid_from")
        return self


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    policy_id: uuid.UUID
    seq: int
    rules: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime


class DecisionRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    known_at: datetime | None = None

    @field_validator("occurred_at", "known_at")
    @classmethod
    def _coerce_tz(cls, v: datetime | None) -> datetime | None:
        return _aware(v) if v is not None else None


class DecisionResponse(BaseModel):
    result: bool
    policy_id: uuid.UUID
    version_id: uuid.UUID
    occurred_at: datetime
    trace: dict[str, Any]
