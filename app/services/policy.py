"""Policy, draft and publishing service logic."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, NotFound, PreconditionFailed
from app.models import OutboxEvent, Policy, PolicyVersion
from app.rules.engine import validate_rule
from app.schemas import PublishRequest
from app.services.time import normalize_utc


async def get_tenant_policy(
    session: AsyncSession, tenant_id: str, policy_id: uuid.UUID, *, for_update: bool = False
) -> Policy:
    """Fetch a policy scoped to the tenant.

    Cross-tenant access raises :class:`NotFound` (404), never 403, so tenants
    cannot probe for the existence of other tenants' resources.
    """
    stmt = select(Policy).where(Policy.id == policy_id, Policy.tenant_id == tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    policy = await session.scalar(stmt)
    if policy is None:
        raise NotFound("policy")
    return policy


async def create_policy(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    rule: dict | None,
) -> Policy:
    if rule is not None:
        validate_rule(rule)
    policy = Policy(tenant_id=tenant_id, name=name, draft_rule=rule, draft_revision=1)
    session.add(policy)
    await session.flush()
    return policy


async def get_draft(session: AsyncSession, tenant_id: str, policy_id: uuid.UUID) -> Policy:
    policy = await get_tenant_policy(session, tenant_id, policy_id)
    if policy.draft_rule is None:
        raise NotFound("draft")
    return policy


async def create_draft(
    session: AsyncSession,
    tenant_id: str,
    policy_id: uuid.UUID,
    rule: dict,
    expected_revision: int,
) -> Policy:
    """Create the (single) draft when no draft exists.

    Publishing consumes the draft, so this endpoint starts the next edit
    cycle. ``If-Match`` must equal the policy's current revision, preventing a
    lost create against a concurrent publish.
    """
    validate_rule(rule)
    policy = await get_tenant_policy(session, tenant_id, policy_id, for_update=True)
    if expected_revision != policy.draft_revision:
        raise PreconditionFailed(
            "policy revision does not match If-Match",
            current_revision=policy.draft_revision,
        )
    if policy.draft_rule is not None:
        raise Conflict("draft_exists", "policy already has an editable draft")
    policy.draft_rule = rule
    policy.draft_revision += 1
    await session.flush()
    return policy


async def update_draft(
    session: AsyncSession,
    tenant_id: str,
    policy_id: uuid.UUID,
    rule: dict,
    expected_revision: int | None,
) -> Policy:
    validate_rule(rule)
    policy = await get_tenant_policy(session, tenant_id, policy_id, for_update=True)
    if policy.draft_rule is None:
        raise NotFound("draft")
    if expected_revision is not None and expected_revision != policy.draft_revision:
        raise PreconditionFailed(
            "draft revision does not match If-Match",
            current_revision=policy.draft_revision,
        )
    policy.draft_rule = rule
    policy.draft_revision += 1
    await session.flush()
    return policy


async def list_versions(
    session: AsyncSession, tenant_id: str, policy_id: uuid.UUID
) -> list[PolicyVersion]:
    # Tenant scoping happens even for the listing: unknown policy -> 404.
    await get_tenant_policy(session, tenant_id, policy_id)
    result = await session.scalars(
        select(PolicyVersion)
        .where(
            PolicyVersion.policy_id == policy_id,
            PolicyVersion.tenant_id == tenant_id,
        )
        .order_by(PolicyVersion.version.asc())
    )
    return list(result)


async def publish_draft(
    session: AsyncSession,
    tenant_id: str,
    policy_id: uuid.UUID,
    request: PublishRequest,
    expected_revision: int | None,
) -> PolicyVersion:
    """Publish the draft as a new immutable version.

    Concurrency strategy:

    1. the parent ``policies`` row is locked ``SELECT ... FOR UPDATE`` so two
       concurrent publishes of the *same* policy serialize at the database;
    2. version numbers are allocated from a ``MAX(version)+1`` under that lock;
    3. effective-interval overlap is additionally guarded by a GiST exclusion
       constraint — a DB-level guarantee that survives even if the row lock
       were bypassed and that catches inserts outside this service path.
    """
    effective_start = normalize_utc(request.effective_start)
    effective_end = (
        normalize_utc(request.effective_end) if request.effective_end is not None else None
    )

    policy = await get_tenant_policy(session, tenant_id, policy_id, for_update=True)
    if policy.draft_rule is None:
        raise NotFound("draft")
    if expected_revision is not None and expected_revision != policy.draft_revision:
        raise PreconditionFailed(
            "draft revision does not match If-Match",
            current_revision=policy.draft_revision,
        )

    next_version = await session.scalar(
        select(func.coalesce(func.max(PolicyVersion.version), 0) + 1).where(
            PolicyVersion.policy_id == policy_id
        )
    )

    version = PolicyVersion(
        policy_id=policy.id,
        tenant_id=tenant_id,
        version=next_version,
        rule=policy.draft_rule,
        effective_start=effective_start,
        effective_end=effective_end,
        revision=policy.draft_revision,
    )
    session.add(version)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(
            "effective_interval_overlap",
            "published version overlaps an existing effective interval",
        ) from exc

    # Publishing consumes the draft: the snapshot is immutable and the client
    # starts a new edit cycle with POST .../draft. The revision token is bumped
    # so a stale client cannot publish/update against the old draft state.
    policy.draft_rule = None
    policy.draft_revision += 1

    from app.config import get_settings

    event = OutboxEvent(
        event_type="policy.published",
        tenant_id=tenant_id,
        aggregate_type="policy",
        aggregate_id=policy.id,
        payload={
            "policy_id": str(policy.id),
            "version": next_version,
            "effective_start": effective_start.isoformat(),
            "effective_end": effective_end.isoformat() if effective_end else None,
        },
        max_attempts=get_settings().outbox_max_attempts,
    )
    session.add(event)
    await session.flush()
    return version
