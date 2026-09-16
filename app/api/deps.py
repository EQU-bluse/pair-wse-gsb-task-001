"""Shared FastAPI dependencies: tenant isolation header and optimistic locks."""

from __future__ import annotations

import re

from fastapi import Header

from app.errors import AppError

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@\-]{0,62}$")
_ETAG_RE = re.compile(r'^(?:W/)?"?(\d+)"?$')


async def require_tenant(x_tenant_id: str | None = Header(default=None)) -> str:
    """Every business request must identify its tenant."""
    if not x_tenant_id or not x_tenant_id.strip():
        raise AppError(400, "missing_tenant", "X-Tenant-ID header is required")
    tenant = x_tenant_id.strip()
    if not _TENANT_RE.match(tenant):
        raise AppError(400, "invalid_tenant", "X-Tenant-ID has an invalid format")
    return tenant


async def optional_idempotency_key(
    idempotency_key: str | None = Header(default=None),
) -> str | None:
    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key or len(key) > 255:
            raise AppError(400, "invalid_idempotency_key", "invalid Idempotency-Key")
        return key
    return None


def parse_if_match(value: str | None) -> int:
    """Parse an ``If-Match`` revision etag such as ``"3"``, ``W/"3"`` or ``3``."""
    if value is None or not value.strip():
        raise AppError(
            428,
            "precondition_required",
            "If-Match header with the current draft revision is required",
        )
    match = _ETAG_RE.match(value.strip())
    if not match:
        raise AppError(400, "invalid_if_match", "If-Match must be a revision etag")
    return int(match.group(1))
