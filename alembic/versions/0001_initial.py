"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Required for the exclusion constraint mixing uuid (=) and tstzrange (&&).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_policies_tenant_name"),
    )
    op.create_index("ix_policies_tenant_id", "policies", ["tenant_id"])

    op.create_table(
        "policy_drafts",
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_policy_drafts_tenant_id", "policy_drafts", ["tenant_id"])

    op.create_table(
        "policy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("policy_id", "seq", name="uq_versions_policy_seq"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_versions_valid_range"
        ),
    )
    op.create_index("ix_policy_versions_policy_id", "policy_versions", ["policy_id"])
    op.create_index("ix_policy_versions_tenant_id", "policy_versions", ["tenant_id"])
    op.create_index(
        "ix_versions_policy_valid", "policy_versions", ["policy_id", "valid_from", "valid_to"]
    )
    # Authoritative no-overlap guard: half-open [valid_from, valid_to) ranges
    # (NULL valid_to = +infinity) must not overlap within one policy.
    op.execute(
        "ALTER TABLE policy_versions ADD CONSTRAINT excl_versions_no_overlap "
        "EXCLUDE USING gist ("
        "policy_id WITH =, "
        "tstzrange(valid_from, valid_to, '[)') WITH &&"
        ")"
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("path", sa.String(512), primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'sent', 'dead')", name="ck_outbox_status"),
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])
    op.create_index("ix_outbox_pending", "outbox_events", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("idempotency_keys")
    op.drop_table("policy_versions")
    op.drop_table("policy_drafts")
    op.drop_table("policies")
