"""Per-minute request, error and latency rollups, updated in the same transaction as the spans.

Three rules keep the numbers right:

1. Only spans that were newly inserted are counted (the insert RETURNs them). A retried batch
   inserts nothing new, so it adds nothing to the counts.
2. Only SERVER spans count as requests. A checkout produces nine spans but is one request
   to each of four services, not nine requests.
3. A span counts in the minute it ended. Spans arriving more than `late_after` after they
   ended are stored but not counted, because alerts may already have read that minute;
   they are counted in a `late` statistic instead, so nothing disappears silently.

Two batches can update the same (service, operation, minute) row at the same time. Postgres
serialises them on the row lock, and the arithmetic happens inside the UPDATE, so neither
overwrites the other. Rows are upserted in sorted key order so that two transactions
touching the same rows always lock them in the same order and can't deadlock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from .latency import bucket_index, empty_buckets
from .models import Base
from .otlp import ns_to_datetime

SPAN_KIND_SERVER = 2
STATUS_ERROR = 2

Key = tuple[str, str, datetime]


@dataclass
class Aggregate:
    requests: int = 0
    errors: int = 0
    buckets: list[int] = field(default_factory=empty_buckets)


@dataclass
class RollupResult:
    aggregates: dict[Key, Aggregate]
    late: int


def aggregate(
    spans: Iterable[dict[str, Any]], now: datetime, late_after: timedelta
) -> RollupResult:
    groups: dict[Key, Aggregate] = {}
    late = 0
    for span in spans:
        if span["kind"] != SPAN_KIND_SERVER:
            continue
        ended = ns_to_datetime(span["start_ns"] + span["duration_ns"])
        if now - ended > late_after:
            late += 1
            continue
        minute = ended.replace(second=0, microsecond=0)
        agg = groups.setdefault((span["service"], span["operation"], minute), Aggregate())
        agg.requests += 1
        agg.errors += span["status_code"] == STATUS_ERROR
        agg.buckets[bucket_index(span["duration_ns"])] += 1
    return RollupResult(groups, late)


# Element-wise addition of the stored and the incoming histogram, inside the UPDATE.
_ADD_BUCKETS = text(
    "ARRAY(SELECT a + b FROM unnest(metric_rollups.latency_buckets, "
    "excluded.latency_buckets) WITH ORDINALITY AS t(a, b, i) ORDER BY i)"
)


async def upsert(conn: AsyncConnection, aggregates: dict[Key, Aggregate]) -> None:
    if not aggregates:
        return
    rows = [
        {
            "service": service,
            "operation": operation,
            "minute": minute,
            "request_count": agg.requests,
            "error_count": agg.errors,
            "latency_buckets": agg.buckets,
        }
        for (service, operation, minute), agg in sorted(aggregates.items())  # lock order
    ]
    table = Base.metadata.tables["metric_rollups"]
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["service", "operation", "minute"],
        set_={
            "request_count": table.c.request_count + stmt.excluded.request_count,
            "error_count": table.c.error_count + stmt.excluded.error_count,
            "latency_buckets": _ADD_BUCKETS,
        },
    )
    await conn.execute(stmt)
