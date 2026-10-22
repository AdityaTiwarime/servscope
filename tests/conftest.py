from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from servscope.app import create_app
from servscope.config import Settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://servscope:servscope@127.0.0.1:5432/servscope_test"
)
TOKEN = "test-ingest-token"
ADMIN_TOKEN = "test-admin-token-0123456789"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _default_rules() -> list[tuple[object, ...]]:
    """The rules migration 0003 seeds, read from the migration itself, so tests start from
    exactly what a fresh install has."""
    path = ROOT / "migrations" / "versions" / "0003_alerting.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.DEFAULT_RULES)


DEFAULT_RULES = _default_rules()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "db_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def db_url() -> str:
    """Migrates the test database once per run, through Alembic, so the migrations
    themselves are what the tests exercise."""
    env = {**os.environ, "SERVSCOPE_DATABASE_URL": TEST_DB}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
    return TEST_DB


@pytest.fixture
async def clean_db(db_url: str) -> AsyncIterator[str]:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE spans, logs, metric_rollups, incidents, incident_events, "
                "alert_states, audit_log, app_settings, alert_rules RESTART IDENTITY CASCADE"
            )
        )
        for name, kind, service, threshold, window_s, for_s, recover_s in DEFAULT_RULES:
            await conn.execute(
                text(
                    "INSERT INTO alert_rules (name, kind, service, threshold, window_s, for_s, "
                    "recover_s) VALUES (:n, :k, :s, :t, :w, :f, :r)"
                ),
                {"n": name, "k": kind, "s": service, "t": threshold, "w": window_s,
                 "f": for_s, "r": recover_s},
            )  # fmt: skip
    await engine.dispose()
    yield db_url


def settings(db_url: str, **overrides: Any) -> Settings:
    overrides.setdefault("retention_enabled", False)  # tests call retention directly
    overrides.setdefault("alerts_enabled", False)  # ...and the alert evaluator
    return Settings(database_url=db_url, ingest_token=SecretStr(TOKEN), **overrides)


@asynccontextmanager
async def client_for(
    app_settings: Settings, fault_transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(app_settings, fault_transport)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = app  # type: ignore[attr-defined]  # tests reach app.state through it
            yield client


@pytest.fixture
def make_client(clean_db: str) -> Callable[..., Any]:
    def factory(fault_transport: httpx.AsyncBaseTransport | None = None, **overrides: Any) -> Any:
        return client_for(settings(clean_db, **overrides), fault_transport)

    return factory


async def count_spans(db_url: str) -> int:
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM spans"))).scalar_one()
    await engine.dispose()
    return int(n)
