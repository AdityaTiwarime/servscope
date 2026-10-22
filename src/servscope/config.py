"""Settings come from environment variables (SERVSCOPE_*), never from code."""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVSCOPE_", env_file=".env", extra="ignore")

    env: Literal["demo", "production"] = "demo"
    database_url: str = "postgresql+asyncpg://servscope:servscope@localhost:5432/servscope"
    db_statement_timeout_ms: int = Field(default=5000, ge=50, le=60_000)
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

    # Resolved incidents and the audit log are kept this long.
    incidents_retention_days: int = Field(default=90, ge=1, le=365)

    # The built dashboard (ui/dist). Unset: ServScope serves only the API.
    ui_dir: Path | None = None

    # Admin API (alert rules, settings, scenarios). Unset: every admin call is refused.
    admin_token: SecretStr | None = None

    # Alerting (design section 6). Each pass looks at data up to `alert_settle_seconds` ago,
    # so spans still on their way through the Collector are not mistaken for silence.
    alerts_enabled: bool = True
    alert_interval_seconds: float = Field(default=15, gt=0, le=300)
    alert_settle_seconds: float = Field(default=30, ge=0, le=300)

    # Failure injection: off unless BOTH env=demo and this flag are set (the first two of the
    # three locks; the admin token is the third). ServScope reaches the demo shop's fault
    # switch at these addresses with `fault_token`.
    failure_injection: bool = False
    fault_token: SecretStr | None = None
    payments_url: str = "http://payments:8083"
    orders_url: str = "http://orders:8081"

    @field_validator("admin_token", "fault_token", mode="before")
    @classmethod
    def empty_means_unset(cls, value: object) -> object:
        # Docker Compose passes `${VAR:-}` as an empty string: that means "not set".
        return None if value == "" else value

    @field_validator("admin_token", "fault_token")
    @classmethod
    def long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def injection_is_demo_only(self) -> "Settings":
        """Refuse to start with failure injection in production, or without its tokens."""
        if self.failure_injection:
            if self.env != "demo":
                raise ValueError(
                    "SERVSCOPE_FAILURE_INJECTION=true is only allowed with SERVSCOPE_ENV=demo"
                )
            if self.admin_token is None or self.fault_token is None:
                raise ValueError(
                    "failure injection needs SERVSCOPE_ADMIN_TOKEN and SERVSCOPE_FAULT_TOKEN"
                )
        return self

    @property
    def scenarios_enabled(self) -> bool:
        return self.env == "demo" and self.failure_injection


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # ingest_token comes from the environment
