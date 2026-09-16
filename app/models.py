"""SQLAlchemy ORM models.

Bitemporal design
-----------------
``policy_versions`` carries two independent time axes per published version:

* ``effective_start`` / ``effective_end`` — business-valid time, half-open
  ``[start, end)`` with ``NULL`` end meaning *open-ended*;
* ``recorded_at`` — system time at which the version became visible.

Overlap of business intervals for versions of the same policy is prevented by
a GiST exclusion constraint (``tstzrange ... WITH &&`` plus ``policy_id WITH
=``), which is enforced by the database even under concurrent inserts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (
        Index("ix_policies_tenant_id", "tenant_id"),
        Index("ix_policies_tenant_id_id", "tenant_id", "id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # At most one editable draft per policy. ``draft_revision`` is the
    # optimistic-concurrency token bumped on every draft save and on publish.
    draft_rule: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    draft_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    versions: Mapped[list[PolicyVersion]] = relationship(
        back_populates="policy", order_by="PolicyVersion.version", lazy="raise"
    )


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_policy_versions_interval_valid",
        ),
        ExcludeConstraint(
            ("policy_id", "="),
            (text("tstzrange(effective_start, effective_end, '[)')"), "&&"),
            name="excl_policy_versions_effective_overlap",
            using="gist",
        ),
        Index("ix_policy_versions_policy_id", "policy_id"),
        Index("ix_policy_versions_tenant_id", "tenant_id"),
        Index(
            "ix_policy_versions_effective",
            "policy_id",
            "effective_start",
            "effective_end",
            "recorded_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Business-valid time axis: [effective_start, effective_end)
    effective_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # System time axis: when the version became visible. The value is produced
    # application-side so it shares one clock with the query layer (known_at);
    # the column still carries a DB-level NOT NULL default as a safety net.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # Draft revision snapshot at publish time.
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    policy: Mapped[Policy] = relationship(back_populates="versions", lazy="raise")


class IdempotencyKey(Base):
    """Stored request/response pair for idempotent create/publish endpoints."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "request_path",
            "idempotency_key",
            name="uq_idempotency_tenant_path_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_path: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Populated in the same transaction as the business write. While the
    # inserting transaction is open the row is locked; replays block on it.
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'dead_letter')",
            name="ck_outbox_events_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_nonneg"),
        Index("ix_outbox_events_status_available", "status", "available_at"),
        Index("ix_outbox_events_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
