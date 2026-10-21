"""Settings come from environment variables (SERVSCOPE_*), never from code."""

from functools import lru_cache
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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # ingest_token comes from the environment
