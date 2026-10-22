"""Settings come from environment variables (SERVSCOPE_*), never from code."""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVSCOPE_", env_file=".env", extra="ignore")

    env: Literal["demo", "production"] = "demo"
    database_url: str = "postgresql+asyncpg://servscope:servscope@localhost:5432/servscope"
    ingest_token: SecretStr

    # Ingest limits. Starting values; Milestone 7 load tests may change them.
    max_body_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    write_slots: int = Field(default=8, gt=0)
    slot_wait_seconds: float = Field(default=1.0, gt=0)
    max_attributes_bytes: int = Field(default=8 * 1024, gt=0)

    # Spans that arrive more than this long after they ended are stored but not counted in
    # metrics, because alerts may already have evaluated that minute.
    late_metrics_after: timedelta = timedelta(minutes=15)

    # Retention (design section 7). The job runs every `retention_interval_seconds`.
    spans_retention_days: int = Field(default=7, ge=1, le=90)
    logs_retention_days: int = Field(default=7, ge=1, le=90)
    rollups_retention_days: int = Field(default=30, ge=1, le=365)
    retention_interval_seconds: float = Field(default=60, gt=0)
    retention_batch_size: int = Field(default=5000, gt=0)
    retention_enabled: bool = True

    # The built dashboard (ui/dist). Unset: ServScope serves only the API.
    ui_dir: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # ingest_token comes from the environment
