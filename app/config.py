"""Application configuration loaded from environment variables (prefix ``APP_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+asyncpg://pds:pds@localhost:5432/pds",
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 10
    log_level: str = "INFO"

    # Outbox worker
    worker_poll_interval: float = 1.0
    outbox_max_attempts: int = 5
    outbox_base_delay: float = 1.0
    outbox_max_delay: float = 300.0

    # Readiness
    readiness_timeout: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
