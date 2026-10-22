"""Read API for the dashboard: services, metrics, dependencies, traces and logs.

Fixed queries are parameterised SQL text; the two searches with optional filters (traces and
logs) are built with SQLAlchemy's query builder. Either way user input only ever travels as
bound parameters, never as SQL text.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import (
    BigInteger,
    DateTime,
    String,
    func,
    literal,
    literal_column,
    not_,
    select,
    text,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnClause

from .latency import N_BUCKETS, percentile
from .models import Log, Span
from .trace_analysis import analyse

SPANS = Span.__table__
LOGS = Log.__table__
SIMPLE: ColumnClause[Any] = literal_column(
    "'simple'::regconfig"
)  # the text-search configuration, as indexed

router = APIRouter(prefix="/api")

MAX_RAW_WINDOW = timedelta(hours=24)  # spans and logs
MAX_METRIC_WINDOW = timedelta(days=30)  # minute rollups
DEFAULT_WINDOW = timedelta(minutes=15)
MAX_POINTS = 1500
HEALTH_WINDOW = timedelta(minutes=5)
NO_DATA_AFTER = timedelta(minutes=2)
DEGRADED_ERROR_RATE = 0.05
DEGRADED_P95_MS = 1000.0
DOWN_ERROR_RATE = 0.5
DOWN_MIN_REQUESTS = 5
STEPS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "1h": timedelta(hours=1)}
SEVERITY_MIN = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID = re.compile(r"^[0-9a-f]{16}$")

From = Annotated[datetime | None, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]
Limit = Annotated[int, Query(ge=1, le=100)]
Name = Annotated[str | None, Query(max_length=256)]


class ApiError(Exception):
    def __init__(
        self, status: int, title: str, detail: str = "", headers: dict[str, str] | None = None
    ):
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail
        self.headers = headers or {}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


def window(start: datetime | None, end: datetime | None, limit: timedelta) -> Window:
    for value in (start, end):
        if value is not None and value.tzinfo is None:
            raise ApiError(400, "Time without a time zone", "use ISO 8601 with Z or an offset")
    end = end or datetime.now(UTC)
    start = start or end - DEFAULT_WINDOW
    if start >= end:
        raise ApiError(400, "Empty time window", "'from' must be before 'to'")
    if end - start > limit:
        raise ApiError(400, "Time window too large", f"at most {limit} for this endpoint")
    return Window(start, end)


def encode_cursor(t: datetime, key: str | int) -> str:
    raw = json.dumps({"t": t.isoformat(), "k": key}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, Any]:
    """A cursor is just a position (time, key). A tampered one can only move the page, so it
    is validated for shape, not signed; anything malformed is a 400."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        t = datetime.fromisoformat(data["t"])
        key = data["k"]
        if t.tzinfo is None or not isinstance(key, str | int):
            raise ValueError
        return t, key
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        raise ApiError(400, "Invalid cursor") from None


def latency(buckets: list[int]) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    for name, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        est = percentile(buckets, q)
        out[name] = None if est is None else {"ms": round(est.ms, 2), "at_least": est.at_least}
    return out


def _conn(request: Request) -> AsyncConnection:
    """A read connection, returned unopened: `async with _conn(request) as conn` opens it and
    always gives it back to the pool."""
    conn: AsyncConnection = request.app.state.engine.connect()
    return conn


# --- services --------------------------------------------------------------------------

_SERVICE_TOTALS = text("""
    SELECT service, sum(request_count)::bigint AS requests, sum(error_count)::bigint AS errors
    FROM metric_rollups WHERE minute >= :start AND minute < :end GROUP BY service
""")
_SERVICE_BUCKETS = text("""
    WITH b AS (
        SELECT r.service, u.i, sum(u.n)::bigint AS n
        FROM metric_rollups r, unnest(r.latency_buckets) WITH ORDINALITY AS u(n, i)
        WHERE r.minute >= :start AND r.minute < :end
        GROUP BY r.service, u.i
    )
    SELECT service, array_agg(n ORDER BY i) AS buckets FROM b GROUP BY service
""")
_LAST_SEEN = text(
    "SELECT service, max(minute) + interval '1 minute' AS last_seen "
    "FROM metric_rollups GROUP BY service"
)


async def service_totals(conn: AsyncConnection, w: Window) -> dict[str, dict[str, Any]]:
    params = {"start": w.start, "end": w.end}
    out: dict[str, dict[str, Any]] = {}
    for row in await conn.execute(_SERVICE_TOTALS, params):
        out[row.service] = {"requests": row.requests, "errors": row.errors, "buckets": None}
    for row in await conn.execute(_SERVICE_BUCKETS, params):
        out.setdefault(row.service, {"requests": 0, "errors": 0})["buckets"] = list(row.buckets)
    for item in out.values():
        item["buckets"] = item.get("buckets") or [0] * N_BUCKETS
    return out


def health(now: datetime, last_seen: datetime | None, recent: dict[str, Any] | None) -> str:
    """Current state from the last few minutes. No data is never reported as healthy."""
    if last_seen is None or now - last_seen > NO_DATA_AFTER or not recent:
        return "no_data"
    requests, errors = recent["requests"], recent["errors"]
    rate = errors / requests if requests else 0.0
    if requests >= DOWN_MIN_REQUESTS and rate >= DOWN_ERROR_RATE:
        return "down"
    p95 = percentile(recent["buckets"], 0.95)
    if rate >= DEGRADED_ERROR_RATE or (p95 is not None and p95.ms >= DEGRADED_P95_MS):
        return "degraded"
    return "healthy"


@router.get("/services")
async def services(request: Request, start: From = None, end: To = None) -> dict[str, Any]:
    w = window(start, end, MAX_METRIC_WINDOW)
    now = datetime.now(UTC)
    async with _conn(request) as conn:
        in_window = await service_totals(conn, w)
        recent = await service_totals(conn, Window(now - HEALTH_WINDOW, now + timedelta(minutes=1)))
        last_seen = {r.service: r.last_seen for r in await conn.execute(_LAST_SEEN)}

    seconds = (w.end - w.start).total_seconds()
    items: list[dict[str, Any]] = []
    for name in sorted(set(in_window) | set(last_seen)):
        t = in_window.get(name, {"requests": 0, "errors": 0, "buckets": [0] * N_BUCKETS})
        items.append(
            {
                "service": name,
                "health": health(now, last_seen.get(name), recent.get(name)),
                "last_seen": last_seen.get(name),
                "requests": t["requests"],
                "errors": t["errors"],
                "error_rate": round(t["errors"] / t["requests"], 4) if t["requests"] else None,
                "requests_per_second": round(t["requests"] / seconds, 3),
                "latency_ms": latency(t["buckets"]),
            }
        )
    return {"from": w.start, "to": w.end, "services": items}


_SERIES = text("""
    WITH r AS (
        SELECT date_bin(:step, minute, timestamptz '2000-01-01') AS bucket_start,
               request_count, error_count, latency_buckets
        FROM metric_rollups
        WHERE service = :service AND minute >= :start AND minute < :end
    ), b AS (
        SELECT bucket_start, u.i, sum(u.n)::bigint AS n
        FROM r, unnest(r.latency_buckets) WITH ORDINALITY AS u(n, i)
        GROUP BY bucket_start, u.i
    )
    SELECT r.bucket_start, sum(request_count)::bigint AS requests,
           sum(error_count)::bigint AS errors,
           (SELECT array_agg(n ORDER BY i) FROM b WHERE b.bucket_start = r.bucket_start)
               AS buckets
    FROM r GROUP BY r.bucket_start ORDER BY r.bucket_start
""")


@router.get("/services/{service}/metrics")
async def service_metrics(
    request: Request,
    service: Annotated[str, Path(max_length=128)],
    start: From = None,
    end: To = None,
    step: Literal["1m", "5m", "1h"] = "1m",
) -> dict[str, Any]:
    w = window(start, end, MAX_METRIC_WINDOW)
    size = STEPS[step]
    if (w.end - w.start) / size > MAX_POINTS:
        raise ApiError(400, "Too many points", f"use a larger step (at most {MAX_POINTS} points)")
    params = {"service": service, "start": w.start, "end": w.end, "step": size}
    async with _conn(request) as conn:
        rows = {r.bucket_start: r for r in await conn.execute(_SERIES, params)}

    # One point per step, with nulls where nothing was recorded: a gap must look like a gap,
    # not like a drop to zero.
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    t = epoch + ((w.start - epoch) // size) * size
    points: list[dict[str, Any]] = []
    while t < w.end:
        row = rows.get(t)
        if row is None:
            points.append(
                {
                    "t": t,
                    "requests": None,
                    "errors": None,
                    "error_rate": None,
                    "requests_per_second": None,
                    "latency_ms": None,
                }
            )
        else:
            points.append(
                {
                    "t": t,
                    "requests": row.requests,
                    "errors": row.errors,
                    "error_rate": round(row.errors / row.requests, 4) if row.requests else None,
                    "requests_per_second": round(row.requests / size.total_seconds(), 3),
                    "latency_ms": latency(list(row.buckets)),
                }
            )
        t += size
    return {"service": service, "step": step, "from": w.start, "to": w.end, "points": points}


_DEPENDENCIES = text("""
    SELECT p.service AS caller, c.service AS callee, count(*) AS calls,
           count(*) FILTER (WHERE c.status_code = 2 OR p.status_code = 2) AS errors
    FROM spans c
    JOIN spans p ON p.trace_id = c.trace_id AND p.span_id = c.parent_span_id
    WHERE c.kind = 2 AND c.start_time >= :start AND c.start_time < :end
      AND p.service <> c.service
    GROUP BY p.service, c.service ORDER BY p.service, c.service
""")


@router.get("/dependencies")
async def dependencies(request: Request, start: From = None, end: To = None) -> dict[str, Any]:
    """Who calls whom, built from the spans themselves: a server span whose parent belongs to
    another service is one call between them. Computed at query time, so it is always right
    for the chosen window, whichever batch each span arrived in.

    A call counts as failed if EITHER side says so: a callee that answered too late can
    report success while the caller had already given up with a timeout."""
    w = window(start, end, MAX_RAW_WINDOW)
    async with _conn(request) as conn:
        rows = await conn.execute(_DEPENDENCIES, {"start": w.start, "end": w.end})
        edges = [
            {
                "caller": r.caller,
                "callee": r.callee,
                "calls": r.calls,
                "errors": r.errors,
                "error_rate": round(r.errors / r.calls, 4),
            }
            for r in rows
        ]
    return {"from": w.start, "to": w.end, "edges": edges}


# --- traces ----------------------------------------------------------------------------


@router.get("/traces")
async def list_traces(
    request: Request,
    start: From = None,
    end: To = None,
    service: Name = None,
    operation: Name = None,
    status: Literal["ok", "error"] | None = None,
    min_duration_ms: Annotated[float | None, Query(ge=0, le=3_600_000)] = None,
    limit: Limit = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """Traces with at least one span matching the filters, newest first."""
    w = window(start, end, MAX_RAW_WINDOW)
    first_start = func.min(SPANS.c.start_time).label("first_start")
    stmt = (
        select(SPANS.c.trace_id, first_start)
        .where(SPANS.c.start_time >= w.start, SPANS.c.start_time < w.end)
        .group_by(SPANS.c.trace_id)
        .order_by(first_start.desc(), SPANS.c.trace_id.desc())
        .limit(limit + 1)
    )
    if service:
        stmt = stmt.where(SPANS.c.service == service)
    if operation:
        stmt = stmt.where(SPANS.c.operation == operation)
    if min_duration_ms is not None:
        stmt = stmt.where(SPANS.c.duration_ns >= int(min_duration_ms * 1_000_000))
    has_error = func.bool_or(SPANS.c.status_code == 2)
    if status == "error":
        stmt = stmt.having(has_error)
    elif status == "ok":
        stmt = stmt.having(not_(has_error))
    if cursor:
        ct, ck = decode_cursor(cursor)
        if not isinstance(ck, str) or not TRACE_ID.match(ck):
            raise ApiError(400, "Invalid cursor")
        # Keyset paging: "everything after the last row I saw", stable while data arrives.
        stmt = stmt.having(
            tuple_(func.min(SPANS.c.start_time), SPANS.c.trace_id)
            < tuple_(literal(ct, DateTime(timezone=True)), literal(ck, String))
        )

    async with _conn(request) as conn:
        page = list(await conn.execute(stmt))
        more = len(page) > limit
        page = page[:limit]
        ids = [r.trace_id for r in page]
        summaries = {r.trace_id: r for r in await conn.execute(_TRACE_SUMMARIES, {"ids": ids})}

    traces = []
    for row in page:
        s = summaries[row.trace_id]
        traces.append(
            {
                "trace_id": row.trace_id,
                "start_time": s.first_start,
                # What the caller waited (the root span), not how long the last span ran:
                # a callee still working after its caller gave up didn't make anyone wait.
                "duration_ms": round(s.root_duration_ns / 1e6, 3),
                "trace_duration_ms": round((s.last_end_ns - s.first_start_ns) / 1e6, 3),
                "root_service": s.root_service,
                "root_operation": s.root_operation,
                "http_status": s.root_http_status,
                "span_count": s.span_count,
                "error_count": s.error_count,
                "services": sorted(s.services),
            }
        )
    next_cursor = encode_cursor(page[-1].first_start, page[-1].trace_id) if more else None
    return {"from": w.start, "to": w.end, "traces": traces, "next_cursor": next_cursor}


_TRACE_SUMMARIES = text("""
    WITH agg AS (
        SELECT trace_id, min(start_time) AS first_start, min(start_ns) AS first_start_ns,
               max(start_ns + duration_ns) AS last_end_ns, count(*) AS span_count,
               count(*) FILTER (WHERE status_code = 2) AS error_count,
               array_agg(DISTINCT service) AS services
        FROM spans WHERE trace_id = ANY(CAST(:ids AS varchar[])) GROUP BY trace_id
    ), root AS (
        SELECT DISTINCT ON (trace_id) trace_id, service AS root_service,
               operation AS root_operation, http_status AS root_http_status,
               duration_ns AS root_duration_ns
        FROM spans WHERE trace_id = ANY(CAST(:ids AS varchar[]))
        ORDER BY trace_id, start_ns, duration_ns DESC
    )
    SELECT * FROM agg JOIN root USING (trace_id)
""")

_TRACE_SPANS = text("""
    SELECT span_id, parent_span_id, service, operation, kind, start_ns, duration_ns,
           status_code, status_message, http_status, attributes, attributes_truncated
    FROM spans WHERE trace_id = :trace_id ORDER BY start_ns
""")
_TRACE_LOG_COUNTS = text(
    "SELECT span_id, count(*) AS n FROM logs WHERE trace_id = :trace_id GROUP BY span_id"
)
_KIND = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_STATUS = {0: "unset", 1: "ok", 2: "error"}


@router.get("/traces/{trace_id}")
async def get_trace(request: Request, trace_id: str) -> dict[str, Any]:
    if not TRACE_ID.match(trace_id):
        raise ApiError(400, "Invalid trace ID", "expected 32 lowercase hex characters")
    async with _conn(request) as conn:
        rows = list(await conn.execute(_TRACE_SPANS, {"trace_id": trace_id}))
        log_counts = {
            r.span_id: r.n for r in await conn.execute(_TRACE_LOG_COUNTS, {"trace_id": trace_id})
        }
    if not rows:
        raise ApiError(404, "Trace not found", "it may not have arrived yet, or has expired")

    by_id = {r.span_id: r for r in rows}
    a = analyse([(r.span_id, r.parent_span_id, r.start_ns, r.duration_ns) for r in rows])
    t0 = min(r.start_ns for r in rows)

    # Depth-first order, children by start time: the order a waterfall is drawn in.
    ordered = []
    stack = list(reversed(a.roots))
    while stack:
        node = stack.pop()
        ordered.append(node)
        stack.extend(reversed(node.children))

    spans = []
    for node in ordered:
        r = by_id[node.span_id]
        spans.append(
            {
                "span_id": r.span_id,
                "parent_span_id": r.parent_span_id,
                "service": r.service,
                "operation": r.operation,
                "kind": _KIND.get(r.kind, "unspecified"),
                # Offsets from the trace start, not absolute nanoseconds: epoch nanoseconds don't
                # fit in a JavaScript number (2^53), offsets do.
                "offset_ns": r.start_ns - t0,
                "duration_ns": r.duration_ns,
                "self_ns": node.self_ns,
                "depth": node.depth,
                "status": _STATUS.get(r.status_code, "unset"),
                "status_message": r.status_message,
                "http_status": r.http_status,
                "error_type": (r.attributes or {}).get("error.type"),
                "attributes": r.attributes,
                "attributes_truncated": r.attributes_truncated,
                "on_critical_path": node.on_critical_path,
                "starts_before_parent": node.starts_before_parent,
                "log_count": log_counts.get(r.span_id, 0),
            }
        )
    end_ns = max(r.start_ns + r.duration_ns for r in rows)
    return {
        "trace_id": trace_id,
        "start_ns": str(t0),  # as a string, for the same 2^53 reason
        "duration_ns": end_ns - t0,
        "span_count": len(rows),
        "error_count": sum(r.status_code == 2 for r in rows),
        "services": sorted({r.service for r in rows}),
        "critical_path": a.critical_path,
        "slowest_span_id": a.slowest_span_id,
        "spans": spans,
    }


# --- logs ------------------------------------------------------------------------------


@router.get("/logs")
async def search_logs(
    request: Request,
    start: From = None,
    end: To = None,
    service: Name = None,
    severity: Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"] | None = None,
    trace_id: Annotated[str | None, Query(max_length=32)] = None,
    span_id: Annotated[str | None, Query(max_length=16)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """Newest first. With a trace_id the time window is optional: one trace's logs are found
    through the trace index, however old they are."""
    if trace_id is not None and not TRACE_ID.match(trace_id):
        raise ApiError(400, "Invalid trace ID")
    if span_id is not None and not SPAN_ID.match(span_id):
        raise ApiError(400, "Invalid span ID")

    stmt = (
        select(
            LOGS.c.id,
            LOGS.c.time,
            LOGS.c.service,
            LOGS.c.severity_text,
            LOGS.c.message,
            LOGS.c.trace_id,
            LOGS.c.span_id,
            LOGS.c.attributes,
        )
        .order_by(LOGS.c.time.desc(), LOGS.c.id.desc())
        .limit(limit + 1)
    )
    if trace_id and start is None and end is None:
        w = None
    else:
        w = window(start, end, MAX_RAW_WINDOW)
        stmt = stmt.where(LOGS.c.time >= w.start, LOGS.c.time < w.end)
    if trace_id:
        stmt = stmt.where(LOGS.c.trace_id == trace_id)
    if span_id:
        stmt = stmt.where(LOGS.c.span_id == span_id)
    if service:
        stmt = stmt.where(LOGS.c.service == service)
    if severity:
        stmt = stmt.where(LOGS.c.severity_number >= SEVERITY_MIN[severity])
    if q and q.strip():
        # Written exactly like the GIN index expression, or Postgres won't use the index.
        stmt = stmt.where(
            func.to_tsvector(SIMPLE, LOGS.c.message).op("@@")(func.plainto_tsquery(SIMPLE, q))
        )
    if cursor:
        ct, ck = decode_cursor(cursor)
        if not isinstance(ck, int):
            raise ApiError(400, "Invalid cursor")
        stmt = stmt.where(
            tuple_(LOGS.c.time, LOGS.c.id)
            < tuple_(literal(ct, DateTime(timezone=True)), literal(ck, BigInteger))
        )

    async with _conn(request) as conn:
        rows = list(await conn.execute(stmt))
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "from": w.start if w else None,
        "to": w.end if w else None,
        "logs": [
            {
                "time": r.time,
                "service": r.service,
                "severity": r.severity_text,
                "message": r.message,
                "trace_id": r.trace_id,
                "span_id": r.span_id,
                "attributes": r.attributes,
            }
            for r in rows
        ],
        "next_cursor": encode_cursor(rows[-1].time, rows[-1].id) if more else None,
    }
