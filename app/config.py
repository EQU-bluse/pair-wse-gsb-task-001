from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/policies"
    log_level: str = "INFO"

    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 10
    worker_max_retries: int = 5
    worker_backoff_base_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
