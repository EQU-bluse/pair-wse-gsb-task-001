"""Decision evaluation service."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.rules.engine import evaluate, validate_rule
from app.schemas import DecisionResponse, TraceNode
from app.services.policy import get_tenant_policy
from app.services.time import find_effective_version, normalize_utc, utcnow


async def decide(
    session: AsyncSession,
    tenant_id: str,
    policy_id: uuid.UUID,
    context: dict,
    occurred_at: datetime | None,
    known_at: datetime | None,
) -> DecisionResponse:
    # 404 for missing or cross-tenant policies.
    await get_tenant_policy(session, tenant_id, policy_id)

    occurred = normalize_utc(occurred_at) if occurred_at else utcnow()
    known = normalize_utc(known_at) if known_at else None

    version = await find_effective_version(
        session,
        policy_id=policy_id,
        occurred_at=occurred,
        known_at=known,
    )

    if version is None:
        # No published rule was visible on the bitemporal point asked for.
        trace = TraceNode(
            op="no_effective_version",
            result=False,
            reason="no published version effective at the requested time",
        )
        return DecisionResponse(
            policy_id=policy_id,
            result=False,
            matched_version=None,
            occurred_at=occurred,
            known_at=known,
            trace=trace,
        )

    # Rules were validated at write time; validate defensively anyway — the
    # engine never executes dynamic code.
    validate_rule(version.rule)
    trace = evaluate(version.rule, context)
    return DecisionResponse(
        policy_id=policy_id,
        result=trace.result,
        matched_version=version.version,
        occurred_at=occurred,
        known_at=known,
        trace=trace,
    )
