"""Idempotency-Key support.

A marker row is inserted in the *same* transaction as the business write and
uniquely keyed by ``(tenant_id, request_path, idempotency_key)``. The response
is recorded on that row before commit. Replays therefore:

* same key + same body  -> stored status code and body are returned;
* same key + other body -> 409;
* concurrent same-key requests -> the database unique index serializes them,
  only one resource is created and the loser replays the stored response.

Request bodies are hashed from a canonical, deterministic JSON serialization.
Only successful (2xx) executions are recorded: a request whose business logic
failed rolls back together with its marker, so its retry is genuinely retried.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, Conflict
from app.models import IdempotencyKey


def canonical_hash(body: BaseModel | dict[str, Any] | list[Any]) -> str:
    if isinstance(body, BaseModel):
        payload: Any = body.model_dump(mode="json")
    else:
        payload = body
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _select_marker(
    session: AsyncSession, tenant_id: str, path: str, key: str, *, lock: bool
):
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.tenant_id == tenant_id,
        IdempotencyKey.request_path == path,
        IdempotencyKey.idempotency_key == key,
    )
    if lock:
        stmt = stmt.with_for_update()
    return stmt


async def _acquire_marker(
    session: AsyncSession, tenant_id: str, path: str, key: str, body_hash: str
) -> IdempotencyKey:
    """Lock the marker for this key, inserting it on the winning transaction."""
    for _attempt in range(3):
        existing = await session.scalar(
            _select_marker(session, tenant_id, path, key, lock=True)
        )
        if existing is not None:
            return existing

        marker = IdempotencyKey(
            tenant_id=tenant_id,
            request_path=path,
            idempotency_key=key,
            request_hash=body_hash,
        )
        try:
            # SAVEPOINT: a unique violation from a concurrent inserter must not
            # poison the surrounding transaction.
            async with session.begin_nested():
                session.add(marker)
                await session.flush()
            return marker
        except IntegrityError:
            # Either the concurrent inserter committed (loop re-selects its
            # row) or it aborted (Postgres lets our insert proceed on retry).
            continue
    raise Conflict("idempotency_contention", "too many concurrent idempotent retries")


async def run_idempotent(
    session: AsyncSession,
    *,
    tenant_id: str,
    path: str,
    key: str,
    body: BaseModel,
    producer: Callable[[], Awaitable[tuple[int, Any]]],
) -> tuple[int, Any, bool]:
    """Run ``producer`` at most once for a key.

    Returns ``(status_code, response_body, was_replay)``. ``producer`` returns
    the status code and a JSON-serializable body; its database changes and the
    idempotency bookkeeping share the caller's transaction. On a non-2xx
    failure ``producer`` raises :class:`AppError` and nothing is recorded.
    """
    body_hash = canonical_hash(body)
    marker = await _acquire_marker(session, tenant_id, path, key, body_hash)

    if marker.response_status is not None:
        if marker.request_hash != body_hash:
            raise Conflict(
                "idempotency_key_conflict",
                "Idempotency-Key was already used with a different request body",
            )
        return marker.response_status, marker.response_body, True

    status_code, response_body = await producer()
    if not 200 <= status_code < 300:  # defensive: producers raise on failure
        raise AppError(status_code, "unexpected_status", "non-success producer result")
    marker.request_hash = body_hash
    marker.response_status = status_code
    marker.response_body = response_body
    await session.flush()
    return status_code, response_body, False
