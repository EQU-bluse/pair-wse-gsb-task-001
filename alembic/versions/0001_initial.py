"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gist enables GiST indexes over scalar columns such as policy_id,
    # which the effective-interval exclusion constraint relies on.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("draft_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "draft_revision",
            sa.BigInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_policies_tenant_id", "policies", ["tenant_id"])
    op.create_index(
        "ix_policies_tenant_id_id", "policies", ["tenant_id", "id"], unique=True
    )

    op.create_table(
        "policy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "policy_id", "version", name="uq_policy_versions_policy_version"
        ),
        sa.CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_policy_versions_interval_valid",
        ),
        postgresql.ExcludeConstraint(
            ("policy_id", "="),
            (sa.text("tstzrange(effective_start, effective_end, '[)')"), "&&"),
            name="excl_policy_versions_effective_overlap",
            using="gist",
        ),
    )
    op.create_index(
        "ix_policy_versions_policy_id", "policy_versions", ["policy_id"]
    )
    op.create_index(
        "ix_policy_versions_tenant_id", "policy_versions", ["tenant_id"]
    )
    op.create_index(
        "ix_policy_versions_effective",
        "policy_versions",
        ["policy_id", "effective_start", "effective_end", "recorded_at"],
    )

    # Published versions are immutable: block UPDATE and DELETE at the database
    # level so no code path (including ad-hoc SQL) can rewrite history. This is
    # a ROW-level trigger; TRUNCATE (used to reset test tables) still works.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION policy_versions_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'policy_versions rows are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_versions_immutable
        BEFORE UPDATE OR DELETE ON policy_versions
        FOR EACH ROW EXECUTE FUNCTION policy_versions_immutable()
        """
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("request_path", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "request_path",
            "idempotency_key",
            name="uq_idempotency_tenant_path_key",
        ),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'dead_letter')",
            name="ck_outbox_events_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_nonneg"),
    )
    op.create_index(
        "ix_outbox_events_status_available", "outbox_events", ["status", "available_at"]
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_tenant_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_table("idempotency_keys")

    op.drop_index("ix_policy_versions_effective", table_name="policy_versions")
    op.drop_index("ix_policy_versions_tenant_id", table_name="policy_versions")
    op.drop_index("ix_policy_versions_policy_id", table_name="policy_versions")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_versions_immutable ON policy_versions")
    op.execute("DROP FUNCTION IF EXISTS policy_versions_immutable()")
    op.drop_table("policy_versions")

    op.drop_index("ix_policies_tenant_id_id", table_name="policies")
    op.drop_index("ix_policies_tenant_id", table_name="policies")
    op.drop_table("policies")
