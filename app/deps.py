from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

TenantID = Annotated[str, Header(alias="X-Tenant-ID", min_length=1, max_length=128)]
TenantDep = TenantID
DbDep = Annotated[AsyncSession, Depends(get_db)]
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match")]
