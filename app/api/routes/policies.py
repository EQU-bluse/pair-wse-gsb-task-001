"""Policy, draft, version-history and decision HTTP routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import optional_idempotency_key, parse_if_match, require_tenant
from app.db import get_session
from app.schemas import (
    DecisionRequest,
    DecisionResponse,
    DraftUpsert,
    DraftView,
    PolicyCreate,
    PolicyView,
    PublishRequest,
    VersionView,
)
from app.services import decision as decision_service
from app.services import policy as policy_service
from app.services.idempotency import run_idempotent

router = APIRouter(prefix="/api/v1/policies", tags=["policies"])


def _etag(revision: int) -> dict[str, str]:
    return {"ETag": f'"{revision}"'}


def _dump_policy(policy) -> dict:
    return PolicyView.model_validate(policy).model_dump(mode="json")


def _dump_draft(policy) -> dict:
    return DraftView(
        policy_id=policy.id,
        revision=policy.draft_revision,
        rule=policy.draft_rule,
        updated_at=policy.updated_at,
    ).model_dump(mode="json")


def _dump_version(version) -> dict:
    return VersionView.model_validate(version).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@router.post("", response_model=PolicyView, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyCreate,
    request: Request,
    tenant: str = Depends(require_tenant),
    idempotency_key: str | None = Depends(optional_idempotency_key),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    async with session.begin():
        if idempotency_key is None:
            policy = await policy_service.create_policy(
                session, tenant, body.name, body.rule
            )
            return JSONResponse(
                status_code=201,
                content=_dump_policy(policy),
                headers=_etag(policy.draft_revision),
            )

        async def producer() -> tuple[int, dict]:
            policy = await policy_service.create_policy(
                session, tenant, body.name, body.rule
            )
            return 201, _dump_policy(policy)

        code, data, replayed = await run_idempotent(
            session,
            tenant_id=tenant,
            path=request.url.path,
            key=idempotency_key,
            body=body,
            producer=producer,
        )
        headers = {"Idempotency-Replayed": "true"} if replayed else {}
        headers.update(_etag(int(data["draft_revision"])))
        return JSONResponse(status_code=code, content=data, headers=headers)


@router.get("/{policy_id}", response_model=PolicyView)
async def get_policy(
    policy_id: uuid.UUID,
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PolicyView:
    async with session.begin():
        policy = await policy_service.get_tenant_policy(session, tenant, policy_id)
        return PolicyView.model_validate(policy)


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


@router.get("/{policy_id}/draft", response_model=DraftView)
async def get_draft(
    policy_id: uuid.UUID,
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> DraftView:
    async with session.begin():
        policy = await policy_service.get_draft(session, tenant, policy_id)
        return DraftView(
            policy_id=policy.id,
            revision=policy.draft_revision,
            rule=policy.draft_rule,
            updated_at=policy.updated_at,
        )


@router.post("/{policy_id}/draft", response_model=DraftView, status_code=201)
async def create_draft(
    policy_id: uuid.UUID,
    body: DraftUpsert,
    if_match: str | None = Header(default=None, alias="If-Match"),
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    # If-Match must carry the policy's current revision (see ETag on GET
    # policy). Publishing consumes the draft and bumps the revision, so a
    # stale client gets 412 rather than silently starting a new edit cycle.
    expected = parse_if_match(if_match)
    async with session.begin():
        policy = await policy_service.create_draft(
            session, tenant, policy_id, body.rule, expected
        )
        return JSONResponse(
            status_code=201,
            content=_dump_draft(policy),
            headers=_etag(policy.draft_revision),
        )


@router.put("/{policy_id}/draft", response_model=DraftView)
async def update_draft(
    policy_id: uuid.UUID,
    body: DraftUpsert,
    if_match: str | None = Header(default=None, alias="If-Match"),
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    expected = parse_if_match(if_match)
    async with session.begin():
        policy = await policy_service.update_draft(
            session, tenant, policy_id, body.rule, expected
        )
        return JSONResponse(
            status_code=200,
            content=_dump_draft(policy),
            headers=_etag(policy.draft_revision),
        )


# ---------------------------------------------------------------------------
# Publishing & versions
# ---------------------------------------------------------------------------


@router.post("/{policy_id}/publish", response_model=VersionView, status_code=201)
async def publish_draft(
    policy_id: uuid.UUID,
    body: PublishRequest,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    tenant: str = Depends(require_tenant),
    idempotency_key: str | None = Depends(optional_idempotency_key),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    expected = parse_if_match(if_match)
    async with session.begin():
        if idempotency_key is None:
            version = await policy_service.publish_draft(
                session, tenant, policy_id, body, expected
            )
            return JSONResponse(
                status_code=201,
                content=_dump_version(version),
                headers=_etag(version.revision),
            )

        async def producer() -> tuple[int, dict]:
            version = await policy_service.publish_draft(
                session, tenant, policy_id, body, expected
            )
            return 201, _dump_version(version)

        code, data, replayed = await run_idempotent(
            session,
            tenant_id=tenant,
            path=request.url.path,
            key=idempotency_key,
            body=body,
            producer=producer,
        )
        headers = {"Idempotency-Replayed": "true"} if replayed else {}
        headers.update(_etag(int(data["revision"])))
        return JSONResponse(status_code=code, content=data, headers=headers)


@router.get("/{policy_id}/versions", response_model=list[VersionView])
async def list_versions(
    policy_id: uuid.UUID,
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[VersionView]:
    async with session.begin():
        versions = await policy_service.list_versions(session, tenant, policy_id)
        return [VersionView.model_validate(v) for v in versions]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@router.post("/{policy_id}/decide", response_model=DecisionResponse)
async def decide(
    policy_id: uuid.UUID,
    body: DecisionRequest,
    tenant: str = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> DecisionResponse:
    async with session.begin():
        return await decision_service.decide(
            session,
            tenant,
            policy_id,
            body.context,
            body.occurred_at,
            body.known_at,
        )
