"""Deletes telemetry older than its retention period.

Deletes run in small batches, each in its own short transaction. One giant DELETE of a
week's spans would hold locks for a long time and bloat the write-ahead log; batches of a few
thousand rows let ingest keep running in between.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Settings
from .stats import Stats

log = logging.getLogger("servscope.retention")

# Table and time column are fixed strings from this list, never from user input.
_TARGETS = (
    ("spans", "start_time", "spans_retention_days"),
    ("logs", "time", "logs_retention_days"),
    ("metric_rollups", "minute", "rollups_retention_days"),
)


async def run_once(engine: AsyncEngine, settings: Settings, now: datetime) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for table, column, setting in _TARGETS:
        cutoff = now - timedelta(days=getattr(settings, setting))
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
        except Exception:  # never let one failed pass (e.g. database down) stop the loop
            log.exception("retention pass failed; will try again")
        await asyncio.sleep(settings.retention_interval_seconds)
