"""Milestone 3: logs, rollups, the read API and retention, against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import ROOT, TOKEN, settings
from opentelemetry.proto.trace.v1.trace_pb2 import Span
from otlp_builders import make_export, make_log, make_logs_export, make_span
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from servscope import retention

AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-protobuf"}
MS = 1_000_000
SERVER, CLIENT, INTERNAL = Span.SPAN_KIND_SERVER, Span.SPAN_KIND_CLIENT, Span.SPAN_KIND_INTERNAL


async def send(client: Any, body: bytes, path: str = "/v1/traces") -> None:
    r = await client.post(path, content=body, headers=AUTH)
    assert r.status_code == 200, r.content


async def service(client: Any, name: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/api/services", params=params)
    assert r.status_code == 200, r.text
    [s] = [s for s in r.json()["services"] if s["service"] == name]
    return s


def now_ns(minus_seconds: float = 0) -> int:
    return time.time_ns() - int(minus_seconds * 1e9)


# --- metric rollups --------------------------------------------------------------------


async def test_server_spans_become_request_and_error_counts(make_client: Any) -> None:
    spans = [make_span(name="POST /orders") for _ in range(8)]
    spans += [make_span(name="POST /orders", error=True) for _ in range(2)]
    spans += [make_span(name="POST", kind=CLIENT), make_span(name="work", kind=INTERNAL)]
    async with make_client() as client:
        await send(client, make_export(spans, service="orders"))
        s = await service(client, "orders")
    # Client and internal spans are not requests to this service.
    assert (s["requests"], s["errors"], s["error_rate"]) == (10, 2, 0.2)


async def test_retried_batch_does_not_double_count(make_client: Any) -> None:
    body = make_export([make_span() for _ in range(5)], service="orders")
    async with make_client() as client:
        await send(client, body)
        await send(client, body)  # the Collector's retry after a lost acknowledgement
        s = await service(client, "orders")
        stats = (await client.get("/metrics")).text
    assert s["requests"] == 5
    assert "servscope_spans_duplicate_total 5" in stats


async def test_concurrent_batches_count_exactly(make_client: Any) -> None:
    # 25 batches land on the same (service, operation, minute) row at once.
    bodies = [make_export([make_span() for _ in range(20)], service="orders") for _ in range(25)]
    async with make_client(write_slots=8, slot_wait_seconds=30) as client:
        await asyncio.gather(*(send(client, b) for b in bodies))
        s = await service(client, "orders")
    assert s["requests"] == 500


async def test_late_spans_are_stored_but_not_counted(make_client: Any, clean_db: str) -> None:
    old = make_span(start_ns=now_ns(minus_seconds=20 * 60))  # ended 20 minutes ago
    async with make_client() as client:
        await send(client, make_export([old, make_span()], service="orders"))
        s = await service(client, "orders", **{"from": iso(minutes=-60)})
        stats = (await client.get("/metrics")).text
        listed = await client.get("/api/traces", params={"from": iso(minutes=-60)})
    assert s["requests"] == 1
    assert "servscope_spans_late_total 1" in stats
    assert len(listed.json()["traces"]) == 2  # the late span is still searchable


async def test_percentiles_come_from_the_histogram(make_client: Any) -> None:
    # 95 fast requests (20 ms) and 5 slow ones (1.5 s).
    spans = [make_span(duration_ns=20 * MS) for _ in range(95)]
    spans += [make_span(duration_ns=1500 * MS) for _ in range(5)]
    async with make_client() as client:
        await send(client, make_export(spans, service="payments"))
        s = await service(client, "payments")
    lat = s["latency_ms"]
    assert 10 < lat["p50"]["ms"] <= 25
    assert lat["p95"]["ms"] <= 25  # the 95th request is still a fast one
    assert 1000 < lat["p99"]["ms"] <= 2500
    assert s["health"] == "healthy"  # 5% slow requests is a p99 problem, not a p95 one


async def test_slow_p95_makes_a_service_degraded(make_client: Any) -> None:
    spans = [make_span(duration_ns=20 * MS) for _ in range(90)]
    spans += [make_span(duration_ns=1500 * MS) for _ in range(10)]
    async with make_client() as client:
        await send(client, make_export(spans, service="payments"))
        s = await service(client, "payments")
    assert s["errors"] == 0
    assert s["latency_ms"]["p95"]["ms"] > 1000
    assert s["health"] == "degraded"


async def test_too_few_requests_give_no_percentiles(make_client: Any) -> None:
    async with make_client() as client:
        await send(client, make_export([make_span() for _ in range(5)], service="tiny"))
        s = await service(client, "tiny")
    assert s["latency_ms"] == {"p50": None, "p95": None, "p99": None}


@pytest.mark.parametrize(("errors", "expected"), [(0, "healthy"), (2, "degraded"), (12, "down")])
async def test_health_follows_recent_error_rate(
    make_client: Any, errors: int, expected: str
) -> None:
    spans = [make_span(error=i < errors) for i in range(20)]
    async with make_client() as client:
        await send(client, make_export(spans, service="orders"))
        assert (await service(client, "orders"))["health"] == expected


async def test_silence_is_no_data_not_healthy(make_client: Any, clean_db: str) -> None:
    engine = create_async_engine(clean_db)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO metric_rollups VALUES ('quiet', 'GET /', :m, 50, 0, :b)"),
            {
                "m": datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10),
                "b": [50] + [0] * 11,
            },
        )
    await engine.dispose()
    async with make_client() as client:
        s = await service(client, "quiet", **{"from": iso(minutes=-30)})
    assert s["requests"] == 50 and s["errors"] == 0
    assert s["health"] == "no_data"  # zero errors, but nothing heard for 10 minutes


async def test_metric_series_has_nulls_for_gaps(make_client: Any) -> None:
    async with make_client() as client:
        await send(client, make_export([make_span() for _ in range(3)], service="orders"))
        r = await client.get(
            "/api/services/orders/metrics", params={"from": iso(minutes=-10), "step": "1m"}
        )
    points = r.json()["points"]
    assert len(points) in (10, 11)  # one per minute, the window edge may split a minute
    filled = [p for p in points if p["requests"] is not None]
    assert [p["requests"] for p in filled] == [3]
    assert all(p["latency_ms"] is None for p in points if p["requests"] is None)


async def test_metric_series_refuses_too_many_points(make_client: Any) -> None:
    async with make_client() as client:
        r = await client.get(
            "/api/services/orders/metrics", params={"from": iso(days=-3), "step": "1m"}
        )
    assert r.status_code == 400
    assert r.headers["content-type"] == "application/problem+json"


# --- dependencies ----------------------------------------------------------------------


async def test_dependency_map_comes_from_real_calls(make_client: Any) -> None:
    tid = os.urandom(16)
    gw, gw_call, od, od_call, pay = (os.urandom(8) for _ in range(5))
    async with make_client() as client:
        # Each service's spans in its own batch, children first, as the Collector may do.
        await send(
            client,
            make_export(
                [make_span(trace_id=tid, span_id=pay, parent=od_call, error=True)], "payments"
            ),
        )
        await send(
            client,
            make_export(
                [
                    make_span(trace_id=tid, span_id=od, parent=gw_call),
                    make_span(trace_id=tid, span_id=od_call, parent=od, kind=CLIENT),
                ],
                "orders",
            ),
        )
        await send(
            client,
            make_export(
                [
                    make_span(trace_id=tid, span_id=gw),
                    make_span(trace_id=tid, span_id=gw_call, parent=gw, kind=CLIENT),
                ],
                "gateway",
            ),
        )
        r = await client.get("/api/dependencies")
    edges = {(e["caller"], e["callee"]): e for e in r.json()["edges"]}
    assert set(edges) == {("gateway", "orders"), ("orders", "payments")}
    assert edges[("orders", "payments")]["errors"] == 1
    assert edges[("gateway", "orders")]["errors"] == 0


async def test_dependency_errors_count_the_callers_view(make_client: Any) -> None:
    # Payments answered 201, but too late: only orders' client span knows the call failed.
    tid, od, od_call, pay = os.urandom(16), os.urandom(8), os.urandom(8), os.urandom(8)
    async with make_client() as client:
        await send(
            client,
            make_export(
                [
                    make_span(trace_id=tid, span_id=od),
                    make_span(trace_id=tid, span_id=od_call, parent=od, kind=CLIENT, error=True),
                ],
                "orders",
            ),
        )
        await send(
            client, make_export([make_span(trace_id=tid, span_id=pay, parent=od_call)], "payments")
        )
        r = await client.get("/api/dependencies")
    [edge] = r.json()["edges"]
    assert (edge["caller"], edge["callee"], edge["errors"]) == ("orders", "payments", 1)


# --- traces ----------------------------------------------------------------------------


async def test_trace_detail_explains_where_the_time_went(make_client: Any) -> None:
    tid = os.urandom(16)
    gw, od, inv, pay = (os.urandom(8) for _ in range(4))
    t0 = now_ns()
    spans_gw = [
        make_span(
            trace_id=tid, span_id=gw, start_ns=t0, duration_ns=100 * MS, name="POST /checkout"
        )
    ]
    spans_od = [
        make_span(
            trace_id=tid,
            span_id=od,
            parent=gw,
            start_ns=t0 + 5 * MS,
            duration_ns=90 * MS,
            name="POST /orders",
        )
    ]
    spans_inv = [
        make_span(
            trace_id=tid,
            span_id=inv,
            parent=od,
            start_ns=t0 + 10 * MS,
            duration_ns=10 * MS,
            name="POST /reservations",
        )
    ]
    spans_pay = [
        make_span(
            trace_id=tid,
            span_id=pay,
            parent=od,
            start_ns=t0 + 25 * MS,
            duration_ns=65 * MS,
            name="POST /charges",
            error=True,
            attrs={"error.type": "payments_timeout"},
        )
    ]
    logs = [
        make_log("charge timed out", trace_id=tid, span_id=pay, severity=17),
        make_log("retrying release", trace_id=tid, span_id=pay, severity=13),
    ]
    async with make_client() as client:
        for spans, svc in (
            (spans_gw, "gateway"),
            (spans_od, "orders"),
            (spans_inv, "inventory"),
            (spans_pay, "payments"),
        ):
            await send(client, make_export(spans, svc))
        await send(client, make_logs_export(logs, "payments"), "/v1/logs")
        r = await client.get(f"/api/traces/{tid.hex()}")

    body = r.json()
    assert body["span_count"] == 4 and body["error_count"] == 1
    assert body["duration_ns"] == 100 * MS
    # Reserve stock, then charge: the request waited on both, one after the other.
    assert set(body["critical_path"]) == {gw.hex(), od.hex(), pay.hex(), inv.hex()}
    assert body["slowest_span_id"] == pay.hex()
    by_id = {s["span_id"]: s for s in body["spans"]}
    assert [s["span_id"] for s in body["spans"]] == [gw.hex(), od.hex(), inv.hex(), pay.hex()]
    assert by_id[pay.hex()]["offset_ns"] == 25 * MS
    assert by_id[pay.hex()]["depth"] == 2
    assert by_id[pay.hex()]["error_type"] == "payments_timeout"
    assert by_id[pay.hex()]["log_count"] == 2
    assert isinstance(body["start_ns"], str)  # epoch nanoseconds exceed a JS number


async def test_trace_list_reports_what_the_caller_waited(make_client: Any) -> None:
    # Orders gives up at 2 s; payments keeps going to 3 s. The customer waited 2 s.
    tid, root, call = os.urandom(16), os.urandom(8), os.urandom(8)
    t0 = now_ns()
    async with make_client() as client:
        await send(
            client,
            make_export(
                [
                    make_span(trace_id=tid, span_id=root, start_ns=t0, duration_ns=2000 * MS),
                    make_span(
                        trace_id=tid,
                        span_id=call,
                        parent=root,
                        start_ns=t0 + 10 * MS,
                        duration_ns=1990 * MS,
                        kind=CLIENT,
                    ),
                ],
                "orders",
            ),
        )
        await send(
            client,
            make_export(
                [
                    make_span(
                        trace_id=tid, parent=call, start_ns=t0 + 11 * MS, duration_ns=3000 * MS
                    )
                ],
                "payments",
            ),
        )
        [t] = (await client.get("/api/traces")).json()["traces"]
    assert t["duration_ms"] == 2000
    assert t["trace_duration_ms"] == 3011


async def test_trace_list_filters(make_client: Any) -> None:
    async with make_client() as client:
        await send(
            client, make_export([make_span(name="POST /orders", duration_ns=5 * MS)], "orders")
        )
        await send(
            client,
            make_export(
                [make_span(name="POST /orders", duration_ns=900 * MS, error=True)], "orders"
            ),
        )
        await send(client, make_export([make_span(name="POST /charges")], "payments"))

        async def count(**params: Any) -> int:
            r = await client.get("/api/traces", params=params)
            assert r.status_code == 200, r.text
            return len(r.json()["traces"])

        assert await count() == 3
        assert await count(service="orders") == 2
        assert await count(service="orders", status="error") == 1
        assert await count(service="orders", status="ok") == 1
        assert await count(min_duration_ms=500) == 1
        assert await count(operation="POST /charges") == 1


async def test_paging_is_stable_while_new_traces_arrive(make_client: Any) -> None:
    base = now_ns(minus_seconds=60)
    first = [make_span(start_ns=base + i * MS) for i in range(25)]
    async with make_client() as client:
        await send(client, make_export(first, "orders"))
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 10}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/api/traces", params=params)).json()
            seen += [t["trace_id"] for t in page["traces"]]
            # New, newer traces arrive between page requests.
            await send(client, make_export([make_span()], "orders"))
            cursor = page["next_cursor"]
            if not cursor:
                break
    originals = {s.trace_id.hex() for s in first}
    assert len(seen) == len(set(seen))  # nothing repeated
    assert originals <= set(seen)  # nothing skipped


async def test_trace_id_must_be_hex(make_client: Any) -> None:
    async with make_client() as client:
        bad = await client.get("/api/traces/not-a-trace-id")
        missing = await client.get(f"/api/traces/{'a' * 32}")
    assert bad.status_code == 400
    assert missing.status_code == 404
    assert missing.json()["title"] == "Trace not found"


# --- logs ------------------------------------------------------------------------------


async def test_trace_to_logs_and_back(make_client: Any) -> None:
    tid, sid = os.urandom(16), os.urandom(8)
    async with make_client() as client:
        await send(client, make_export([make_span(trace_id=tid, span_id=sid)], "orders"))
        await send(
            client,
            make_logs_export(
                [
                    make_log("order placed", trace_id=tid, span_id=sid),
                    make_log("unrelated line"),
                ]
            ),
            "/v1/logs",
        )
        logs = (await client.get("/api/logs", params={"trace_id": tid.hex()})).json()["logs"]
        assert [line["message"] for line in logs] == ["order placed"]
        # ...and from the log line straight back to its trace.
        back = await client.get(f"/api/traces/{logs[0]['trace_id']}")
    assert back.status_code == 200
    assert back.json()["spans"][0]["span_id"] == sid.hex()


async def test_secrets_never_reach_the_database(make_client: Any, clean_db: str) -> None:
    line = make_log(
        "login failed password=hunter2 card 4111 1111 1111 1111",
        attrs={"authorization": "Bearer abc", "user": "asha"},
    )
    async with make_client() as client:
        await send(client, make_logs_export([line]), "/v1/logs")
    engine = create_async_engine(clean_db)
    async with engine.connect() as conn:
        message, attrs = (await conn.execute(text("SELECT message, attributes FROM logs"))).one()
    await engine.dispose()
    assert "hunter2" not in message and "4111" not in message
    assert attrs == {"authorization": "[redacted]", "user": "asha"}


async def test_log_search_by_text_severity_and_service(make_client: Any) -> None:
    async with make_client() as client:
        await send(
            client,
            make_logs_export(
                [
                    make_log("payment timed out after 2s", severity=17),
                    make_log("payment authorised", severity=9),
                ],
                "payments",
            ),
            "/v1/logs",
        )
        await send(
            client, make_logs_export([make_log("payment request sent")], "orders"), "/v1/logs"
        )

        async def msgs(**params: Any) -> set[str]:
            r = await client.get("/api/logs", params=params)
            assert r.status_code == 200, r.text
            return {line["message"] for line in r.json()["logs"]}

        assert len(await msgs(q="payment")) == 3
        assert await msgs(q="timed out") == {"payment timed out after 2s"}
        assert await msgs(severity="ERROR") == {"payment timed out after 2s"}
        assert await msgs(service="orders") == {"payment request sent"}


async def test_retried_log_batch_is_stored_once(make_client: Any, clean_db: str) -> None:
    body = make_logs_export([make_log("one"), make_log("two")])
    async with make_client() as client:
        await send(client, body, "/v1/logs")
        await send(client, body, "/v1/logs")
        r = await client.get("/api/logs")
    assert len(r.json()["logs"]) == 2


async def test_log_paging_has_no_gaps_or_repeats(make_client: Any) -> None:
    base = now_ns(minus_seconds=30)
    lines = [make_log(f"line {i}", time_ns=base + i) for i in range(23)]
    async with make_client() as client:
        await send(client, make_logs_export(lines), "/v1/logs")
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 5, **({"cursor": cursor} if cursor else {})}
            page = (await client.get("/api/logs", params=params)).json()
            seen += [line["message"] for line in page["logs"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
    assert seen == [f"line {i}" for i in range(22, -1, -1)]


# --- input limits ----------------------------------------------------------------------


def iso(**delta: float) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


@pytest.mark.parametrize(
    ("path", "params", "status"),
    [
        ("/api/traces", {"from": iso(days=-3)}, 400),  # raw window over 24 h
        ("/api/logs", {"from": iso(days=-3)}, 400),
        ("/api/traces", {"from": "2026-01-01T00:00:00"}, 400),  # no time zone
        ("/api/traces", {"from": iso(), "to": iso(minutes=-5)}, 400),  # backwards
        ("/api/traces", {"limit": 101}, 422),
        ("/api/traces", {"cursor": "not!a!cursor"}, 400),
        ("/api/logs", {"trace_id": "xyz"}, 400),
        ("/api/logs", {"severity": "LOUD"}, 422),
        ("/api/services", {"from": iso(days=-31)}, 400),  # metrics window over 30 days
    ],
)
async def test_bad_queries_are_refused_with_problem_details(
    make_client: Any, path: str, params: dict[str, Any], status: int
) -> None:
    async with make_client() as client:
        r = await client.get(path, params=params)
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["title"]


async def test_sql_injection_in_filters_is_just_text(make_client: Any) -> None:
    async with make_client() as client:
        await send(client, make_export([make_span()], "orders"))
        r = await client.get("/api/traces", params={"service": "x' OR '1'='1"})
        s = await client.get("/api/logs", params={"q": "'); DROP TABLE logs; --"})
        still_there = await client.get("/api/traces")
    assert r.status_code == 200 and r.json()["traces"] == []
    assert s.status_code == 200
    assert len(still_there.json()["traces"]) == 1


# --- retention -------------------------------------------------------------------------


async def test_retention_deletes_only_expired_rows(clean_db: str) -> None:
    now = datetime.now(UTC)
    engine = create_async_engine(clean_db)
    old_span, new_span = now - timedelta(days=8), now - timedelta(days=6)
    async with engine.begin() as conn:
        for i, when in enumerate([old_span] * 5 + [new_span] * 3):
            await conn.execute(
                text(
                    "INSERT INTO spans (trace_id, span_id, service, operation, kind, start_time, "
                    "start_ns, duration_ns, status_code, attributes) VALUES "
                    "(:t, :s, 'orders', 'op', 2, :when, 0, 1, 0, '{}')"
                ),
                {"t": f"{i:032x}", "s": f"{i:016x}", "when": when},
            )
        for i, when in enumerate([now - timedelta(days=8), now - timedelta(hours=1)]):
            await conn.execute(
                text(
                    "INSERT INTO logs (fingerprint, time, time_ns, service, severity_number, "
                    "severity_text, message, attributes) "
                    "VALUES (:f, :when, 0, 'orders', 9, 'INFO', 'm', '{}')"
                ),
                {"f": bytes([i]) * 16, "when": when},
            )
        for when in (now - timedelta(days=31), now - timedelta(days=29)):
            await conn.execute(
                text("INSERT INTO metric_rollups VALUES ('orders', 'op', :when, 1, 0, :b)"),
                {"when": when, "b": [1] + [0] * 11},
            )

        # Incidents: only RESOLVED ones expire. One opened long ago but still open must stay.
        for state, opened, resolved in (
            ("resolved", now - timedelta(days=100), now - timedelta(days=91)),
            ("resolved", now - timedelta(days=100), now - timedelta(days=89)),
            ("open", now - timedelta(days=100), None),
        ):
            await conn.execute(
                text(
                    "INSERT INTO incidents (rule_name, kind, service, threshold, state, "
                    "opened_at, resolved_at) VALUES ('r', 'error_rate', 'orders', 0.1, :s, :o, :r)"
                ),
                {"s": state, "o": opened, "r": resolved},
            )
        for when in (now - timedelta(days=91), now - timedelta(days=1)):
            await conn.execute(
                text(
                    "INSERT INTO audit_log (time, actor, action, details) "
                    "VALUES (:t, 'a', 'x', '{}')"
                ),
                {"t": when},
            )

    # A batch size of 2 makes the job loop, which is the path a big backlog takes.
    deleted = await retention.run_once(engine, settings(clean_db, retention_batch_size=2), now)
    async with engine.connect() as conn:
        left = {
            t: (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()  # noqa: S608
            for t in ("spans", "logs", "metric_rollups", "incidents", "audit_log")
        }
    await engine.dispose()

    assert deleted == {
        "spans": 5, "logs": 1, "metric_rollups": 1, "incidents": 1, "audit_log": 1
    }  # fmt: skip
    assert left == {"spans": 3, "logs": 1, "metric_rollups": 1, "incidents": 2, "audit_log": 1}


async def test_retention_uses_days_saved_from_the_settings_screen(clean_db: str) -> None:
    engine = create_async_engine(clean_db)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        for when in (now - timedelta(days=3), now - timedelta(days=1)):
            await conn.execute(
                text("INSERT INTO metric_rollups VALUES ('orders', 'op', :when, 1, 0, :b)"),
                {"when": when, "b": [1] + [0] * 11},
            )
        # Saved from the dashboard: keep rollups for 2 days instead of the default 30. An
        # out-of-range value someone wrote into the table by hand is ignored.
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES "
                "('retention', '{\"rollups\": 2, \"spans\": 5000}')"
            )
        )
    deleted = await retention.run_once(engine, settings(clean_db), now)
    async with engine.connect() as conn:
        days = await retention.effective_days(conn, settings(clean_db))
    await engine.dispose()
    assert deleted["metric_rollups"] == 1
    assert days["rollups"] == 2 and days["spans"] == 7


def test_models_and_migrations_agree(db_url: str) -> None:
    """`alembic check` fails if the SQLAlchemy models describe a schema the migrations
    don't create, which is how a forgotten migration would reach production."""
    env = {**os.environ, "SERVSCOPE_DATABASE_URL": db_url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
