import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.deps import DbDep, IdempotencyKeyHeader, IfMatchHeader, TenantDep
from app.errors import AppError
from app.idempotency import run_idempotent
from app.models import OutboxEvent, Policy, PolicyDraft, PolicyVersion
from app.rules import evaluate
from app.schemas import (
    DecisionRequest,
    DecisionResponse,
    DraftResponse,
    DraftUpdate,
    PolicyCreate,
    PolicyResponse,
    PublishRequest,
    VersionResponse,
)

router = APIRouter(prefix="/policies", tags=["policies"])


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise AppError(428, "PRECONDITION_REQUIRED", "If-Match header is required")
    raw = if_match.strip()
    if raw.startswith("W/"):
        raise AppError(412, "INVALID_IF_MATCH", "weak entity tags are not accepted")
    raw = raw.strip('"')
    try:
        return int(raw)
    except ValueError:
        raise AppError(412, "INVALID_IF_MATCH", f"invalid If-Match value: {if_match!r}") from None


async def _get_policy_or_404(
    db, policy_id: uuid.UUID, tenant_id: str, lock: bool = False
) -> Policy:
    stmt = select(Policy).where(Policy.id == policy_id, Policy.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    policy = (await db.execute(stmt)).scalar_one_or_none()
    if policy is None:
        raise AppError(404, "POLICY_NOT_FOUND", "policy not found")
    return policy


def _draft_payload(draft: PolicyDraft) -> dict:
    return DraftResponse(
        policy_id=draft.policy_id,
        revision=draft.revision,
        rules=draft.rules,
        updated_at=draft.updated_at,
    ).model_dump(mode="json")


@router.post("", status_code=201)
async def create_policy(
    request: Request,
    body: PolicyCreate,
    tenant_id: TenantDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    async def execute() -> tuple[int, dict]:
        policy = Policy(tenant_id=tenant_id, name=body.name)
        db.add(policy)
        try:
            await db.flush()
        except IntegrityError:
            raise AppError(
                409, "POLICY_NAME_CONFLICT", f"policy named {body.name!r} already exists"
            ) from None
        await db.refresh(policy)
        return 201, PolicyResponse.model_validate(policy).model_dump(mode="json")

    return await run_idempotent(db, tenant_id, request, idempotency_key, execute)


@router.get("/{policy_id}")
async def get_policy(policy_id: uuid.UUID, tenant_id: TenantDep, db: DbDep) -> PolicyResponse:
    policy = await _get_policy_or_404(db, policy_id, tenant_id)
    return PolicyResponse.model_validate(policy)


@router.put("/{policy_id}/draft")
async def put_draft(
    policy_id: uuid.UUID,
    body: DraftUpdate,
    tenant_id: TenantDep,
    db: DbDep,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    policy = await _get_policy_or_404(db, policy_id, tenant_id)
    rules = body.rules.model_dump(mode="json")

    # Lock the draft row so concurrent updates serialize on the database.
    draft = (
        await db.execute(
            select(PolicyDraft).where(PolicyDraft.policy_id == policy.id).with_for_update()
        )
    ).scalar_one_or_none()

    if draft is None:
        # Creating the first draft: If-Match is optional, but if given must be 0.
        if if_match is not None and _parse_if_match(if_match) != 0:
            raise AppError(412, "REVISION_MISMATCH", "draft does not exist yet")
        draft = PolicyDraft(policy_id=policy.id, tenant_id=tenant_id, rules=rules, revision=1)
        db.add(draft)
        try:
            await db.flush()
        except IntegrityError:
            raise AppError(409, "DRAFT_CONFLICT", "draft was created concurrently") from None
        await db.refresh(draft)
    else:
        expected = _parse_if_match(if_match)
        if expected != draft.revision:
            raise AppError(
                412,
                "REVISION_MISMATCH",
                f"draft revision is {draft.revision}, If-Match said {expected}",
            )
        draft.rules = rules
        draft.revision += 1
        await db.flush()
        await db.refresh(draft)

    await db.commit()
    return JSONResponse(content=_draft_payload(draft), headers={"ETag": f'"{draft.revision}"'})


@router.get("/{policy_id}/draft")
async def get_draft(policy_id: uuid.UUID, tenant_id: TenantDep, db: DbDep) -> JSONResponse:
    policy = await _get_policy_or_404(db, policy_id, tenant_id)
    draft = await db.get(PolicyDraft, policy.id)
    if draft is None:
        raise AppError(404, "DRAFT_NOT_FOUND", "policy has no draft")
    return JSONResponse(content=_draft_payload(draft), headers={"ETag": f'"{draft.revision}"'})


@router.post("/{policy_id}/publish", status_code=201)
async def publish_draft(
    request: Request,
    policy_id: uuid.UUID,
    body: PublishRequest,
    tenant_id: TenantDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    async def execute() -> tuple[int, dict]:
        # Lock the policy row: serializes concurrent publishes of this policy.
        policy = await _get_policy_or_404(db, policy_id, tenant_id, lock=True)

        draft = await db.get(PolicyDraft, policy.id)
        if draft is None:
            raise AppError(404, "DRAFT_NOT_FOUND", "policy has no draft to publish")

        expected = _parse_if_match(if_match)
        if expected != draft.revision:
            raise AppError(
                412,
                "REVISION_MISMATCH",
                f"draft revision is {draft.revision}, If-Match said {expected}",
            )

        next_seq = (
            await db.execute(
                select(func.coalesce(func.max(PolicyVersion.seq), 0)).where(
                    PolicyVersion.policy_id == policy.id
                )
            )
        ).scalar_one() + 1

        version = PolicyVersion(
            policy_id=policy.id,
            tenant_id=tenant_id,
            seq=next_seq,
            rules=draft.rules,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
        )
        db.add(version)
        try:
            await db.flush()
        except IntegrityError as exc:
            # The exclusion constraint (no overlapping validity ranges) and the
            # (policy_id, seq) unique constraint are the authoritative guards.
            # asyncpg surfaces the constraint name on the chained cause.
            cause = getattr(exc, "orig", None)
            constraint = ""
            while cause is not None:
                constraint = getattr(cause, "constraint_name", None) or constraint
                cause = getattr(cause, "__cause__", None)
            if "overlap" in constraint:
                raise AppError(
                    409,
                    "VERSION_OVERLAP",
                    "validity interval overlaps an existing published version",
                ) from None
            raise AppError(
                409, "VERSION_CONFLICT", "concurrent publish detected, please retry"
            ) from None
        await db.refresh(version)

        draft.revision += 1

        event = OutboxEvent(
            tenant_id=tenant_id,
            event_type="policy.published",
            payload={
                "policy_id": str(policy.id),
                "version_id": str(version.id),
                "seq": version.seq,
                "valid_from": version.valid_from.isoformat(),
                "valid_to": version.valid_to.isoformat() if version.valid_to else None,
            },
        )
        db.add(event)

        return 201, VersionResponse.model_validate(version).model_dump(mode="json")

    return await run_idempotent(db, tenant_id, request, idempotency_key, execute)


@router.get("/{policy_id}/versions")
async def list_versions(
    policy_id: uuid.UUID,
    tenant_id: TenantDep,
    db: DbDep,
    known_at: Annotated[datetime | None, Query()] = None,
) -> list[VersionResponse]:
    await _get_policy_or_404(db, policy_id, tenant_id)
    stmt = (
        select(PolicyVersion)
        .where(PolicyVersion.policy_id == policy_id)
        .order_by(PolicyVersion.seq)
    )
    if known_at is not None:
        if known_at.tzinfo is None:
            known_at = known_at.replace(tzinfo=UTC)
        stmt = stmt.where(PolicyVersion.recorded_at <= known_at)
    versions = (await db.execute(stmt)).scalars().all()
    return [VersionResponse.model_validate(v) for v in versions]


@router.post("/{policy_id}/decisions")
async def decide(
    policy_id: uuid.UUID,
    body: DecisionRequest,
    tenant_id: TenantDep,
    db: DbDep,
) -> DecisionResponse:
    await _get_policy_or_404(db, policy_id, tenant_id)

    occurred_at = body.occurred_at or datetime.now().astimezone()

    stmt = select(PolicyVersion).where(
        PolicyVersion.policy_id == policy_id,
        PolicyVersion.valid_from <= occurred_at,
        or_(PolicyVersion.valid_to.is_(None), PolicyVersion.valid_to > occurred_at),
    )
    if body.known_at is not None:
        stmt = stmt.where(PolicyVersion.recorded_at <= body.known_at)
    stmt = stmt.order_by(PolicyVersion.recorded_at.desc()).limit(1)

    version = (await db.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise AppError(
            404, "VERSION_NOT_FOUND", "no published version is valid for the requested time"
        )

    trace = evaluate(version.rules, body.context)
    return DecisionResponse(
        result=trace["result"],
        policy_id=policy_id,
        version_id=version.id,
        occurred_at=occurred_at,
        trace=trace,
    )
