import hashlib
import json
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import IdempotencyKey


def hash_request_body(raw_body: bytes) -> str:
    """Stable, deterministic hash of the request body.

    The body is parsed as JSON and re-serialized with sorted keys and tight
    separators so semantically identical payloads hash identically regardless
    of key order or whitespace.
    """
    if raw_body:
        data = json.loads(raw_body)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    else:
        canonical = ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def run_idempotent(
    db: AsyncSession,
    tenant_id: str,
    request: Request,
    key: str | None,
    execute: Callable[[], Awaitable[tuple[int, dict]]],
) -> JSONResponse:
    """Run `execute` exactly once per (tenant, path, Idempotency-Key).

    The idempotency record is written in the same transaction as the business
    writes. Concurrent replays block on the primary key until the first
    transaction commits, then observe the stored response — so a replay can
    never create a second resource.
    """
    if key is None:
        try:
            status, payload = await execute()
        except AppError:
            await db.rollback()
            raise
        await db.commit()
        return JSONResponse(status_code=status, content=payload)

    request_hash = hash_request_body(await request.body())
    path = request.url.path

    record = IdempotencyKey(tenant_id=tenant_id, path=path, key=key, request_hash=request_hash)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(IdempotencyKey, (tenant_id, path, key))
        if existing is None:  # pragma: no cover - defensive
            raise AppError(500, "INTERNAL_ERROR", "idempotency record lookup failed") from None
        if existing.request_hash != request_hash:
            raise AppError(
                409,
                "IDEMPOTENCY_KEY_CONFLICT",
                "Idempotency-Key was already used with a different request body",
            ) from None
        if existing.response_status is None:
            raise AppError(
                409,
                "IDEMPOTENCY_REQUEST_IN_PROGRESS",
                "a request with this Idempotency-Key is still being processed",
            ) from None
        return JSONResponse(status_code=existing.response_status, content=existing.response_body)

    try:
        status, payload = await execute()
    except AppError:
        await db.rollback()
        raise
    record.response_status = status
    record.response_body = payload
    await db.commit()
    return JSONResponse(status_code=status, content=payload)
