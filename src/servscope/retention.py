"""Deletes telemetry older than its retention period.

Deletes run in small batches, each in its own short transaction. One giant DELETE of a
week's spans would hold locks for a long time and bloat the write-ahead log; batches of a few
thousand rows let ingest keep running in between.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .config import Settings
from .stats import Stats

log = logging.getLogger("servscope.retention")

# Table and time column are fixed strings from this list, never from user input. Incidents
# are deleted only once resolved (resolved_at is NULL while open, so never < cutoff).
_TARGETS = (
    ("spans", "start_time", "spans"),
    ("logs", "time", "logs"),
    ("metric_rollups", "minute", "rollups"),
    ("incidents", "resolved_at", "incidents"),
    ("audit_log", "time", "incidents"),
)
# What the Settings screen may change, and within which limits (days).
EDITABLE = {"spans": (1, 90), "logs": (1, 90), "rollups": (1, 365)}
SETTINGS_KEY = "retention"


async def effective_days(conn: AsyncConnection, settings: Settings) -> dict[str, int]:
    """Retention days in force: the environment's defaults, overridden by whatever was saved
    from the Settings screen."""
    days = {
        "spans": settings.spans_retention_days,
        "logs": settings.logs_retention_days,
        "rollups": settings.rollups_retention_days,
        "incidents": settings.incidents_retention_days,
    }
    saved = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = :k"), {"k": SETTINGS_KEY}
    )
    overrides: dict[str, Any] = saved.scalar_one_or_none() or {}
    for name, (low, high) in EDITABLE.items():
        value = overrides.get(name)
        if isinstance(value, int) and low <= value <= high:  # ignore anything out of range
            days[name] = value
    return days


async def run_once(engine: AsyncEngine, settings: Settings, now: datetime) -> dict[str, int]:
    async with engine.connect() as conn:
        days = await effective_days(conn, settings)
    deleted: dict[str, int] = {}
    for table, column, setting in _TARGETS:
        cutoff = now - timedelta(days=days[setting])
        # ctid is the row's physical address: the cheapest way to delete "these N rows".
        stmt = text(
            f"DELETE FROM {table} WHERE ctid IN "  # noqa: S608  # table/column are constants
            f"(SELECT ctid FROM {table} WHERE {column} < :cutoff LIMIT :batch)"
        )
        total = 0
        while True:
            async with engine.begin() as conn:
                result = await conn.execute(
                    stmt, {"cutoff": cutoff, "batch": settings.retention_batch_size}
                )
            total += result.rowcount
            if result.rowcount < settings.retention_batch_size:
                break
        deleted[table] = total
    return deleted


async def run_forever(engine: AsyncEngine, settings: Settings, stats: Stats) -> None:
    while True:
        try:
            deleted = await run_once(engine, settings, datetime.now().astimezone())
            if any(deleted.values()):
                log.info("retention removed rows: %s", deleted)
            stats.add("servscope_retention_deleted_total", sum(deleted.values()))
            stats.mark("retention_ok")
        except Exception:  # never let one failed pass (e.g. database down) stop the loop
            log.exception("retention pass failed; will try again")
            stats.mark("retention_failed")
        await asyncio.sleep(settings.retention_interval_seconds)
