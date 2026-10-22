"""Milestone 5: the alert evaluator against a real PostgreSQL, with a controlled clock.

Data is written straight into the rollup and span tables at chosen minutes, and each pass is
run with an explicit `now`, so every test walks through an incident minute by minute
without waiting for real time to pass.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from servscope import evaluator
from servscope.latency import N_BUCKETS

NO_SETTLE = timedelta(0)
MIN = timedelta(minutes=1)
# Minute-aligned, half an hour ago: inside every look-back, and clear of the real clock.
T = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=30)


@pytest.fixture
async def engine(clean_db: str) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(clean_db)
    yield e
    await e.dispose()


async def sql(engine: AsyncEngine, statement: str, **params: Any) -> list[Any]:
    async with engine.begin() as conn:
        result = await conn.execute(text(statement), params)
        return list(result) if result.returns_rows else []


async def minutes(
    engine: AsyncEngine, service: str, first: int, last: int, requests: int, errors: int
) -> None:
    """One rollup row per minute T+first .. T+last."""
    for m in range(first, last + 1):
        await sql(
            engine,
            "INSERT INTO metric_rollups VALUES (:s, 'op', :m, :r, :e, :b)",
            s=service, m=T + m * MIN, r=requests, e=errors, b=[requests] + [0] * (N_BUCKETS - 1),
        )  # fmt: skip


async def span(
    engine: AsyncEngine, service: str, at: datetime, *, kind: int = 2, error: bool = True,
    attributes: str = "{}",
) -> str:  # fmt: skip
    trace_id = os.urandom(16).hex()
    await sql(
        engine,
        "INSERT INTO spans (trace_id, span_id, service, operation, kind, start_time, start_ns, "
        "duration_ns, status_code, attributes) VALUES (:t, :sp, :s, 'op', :k, :at, 0, 1000, "
        ":st, CAST(:a AS jsonb))",
        t=trace_id, sp=os.urandom(8).hex(), s=service, k=kind, at=at, st=2 if error else 1,
        a=attributes,
    )  # fmt: skip
    return trace_id


async def only(engine: AsyncEngine, rule_name: str) -> None:
    """Disable every seeded rule except one, so a test sees just the incidents it is about."""
    await sql(engine, "UPDATE alert_rules SET enabled = (name = :n)", n=rule_name)


async def tick(engine: AsyncEngine, at: datetime) -> evaluator.PassResult:
    return await evaluator.run_once(engine, at, NO_SETTLE)


async def incidents(engine: AsyncEngine) -> list[Any]:
    return await sql(engine, "SELECT * FROM incidents ORDER BY id")


async def events(engine: AsyncEngine, incident_id: int) -> list[Any]:
    return await sql(
        engine,
        "SELECT type, value, detail, sample_trace_ids FROM incident_events "
        "WHERE incident_id = :i ORDER BY time, id",
        i=incident_id,
    )


async def state(engine: AsyncEngine, service: str = "gateway") -> str:
    [row] = await sql(engine, "SELECT state FROM alert_states WHERE service = :s", s=service)
    return str(row.state)


# "Checkout errors": gateway error rate > 10% over 2 min, for 30 s, recovers after 60 s.


async def test_an_error_spike_opens_one_incident_with_evidence_and_it_resolves(
    engine: AsyncEngine,
) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 4, requests=20, errors=10)  # 50% errors
    await minutes(engine, "gateway", 5, 12, requests=20, errors=0)
    failing = [await span(engine, "gateway", T + MIN + i * timedelta(seconds=5)) for i in range(3)]

    await tick(engine, T + 2 * MIN)
    assert await state(engine) == "pending" and await incidents(engine) == []
    await tick(engine, T + 2 * MIN + timedelta(seconds=15))
    assert await incidents(engine) == []  # one bad reading is not enough
    result = await tick(engine, T + 2 * MIN + timedelta(seconds=30))
    assert result.opened == 1

    [incident] = await incidents(engine)
    assert (incident.state, incident.service, incident.rule_name) == (
        "open", "gateway", "Checkout errors",
    )  # fmt: skip
    [opened] = await events(engine, incident.id)
    assert opened.type == "opened" and opened.value == pytest.approx(0.5)
    assert set(opened.sample_trace_ids) == set(failing)  # the traces to investigate
    assert opened.detail == "error rate above 10% over 2 min, for 30 s"

    # Errors stop at T+5. Once the 2 min window holds only good minutes it recovers, and
    # 60 s later the incident resolves.
    await tick(engine, T + 7 * MIN + timedelta(seconds=30))
    assert await state(engine) == "recovering"
    await tick(engine, T + 8 * MIN)
    assert (await incidents(engine))[0].state == "open"
    result = await tick(engine, T + 8 * MIN + timedelta(seconds=30))
    assert result.resolved == 1
    [incident] = await incidents(engine)
    assert incident.state == "resolved" and incident.resolved_at is not None
    assert [e.type for e in await events(engine, incident.id)] == ["opened", "resolved"]


async def test_a_long_outage_is_one_incident_not_many(engine: AsyncEngine) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 30, requests=20, errors=10)
    for i in range(80):  # 20 minutes of passes, all breaching
        await tick(engine, T + 2 * MIN + i * timedelta(seconds=15))
    [incident] = await incidents(engine)
    assert incident.state == "open"
    assert [e.type for e in await events(engine, incident.id)] == ["opened"]


async def test_breaching_again_while_recovering_stays_the_same_incident(
    engine: AsyncEngine,
) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 2, requests=20, errors=10)
    await minutes(engine, "gateway", 3, 4, requests=20, errors=0)
    await minutes(engine, "gateway", 5, 6, requests=20, errors=10)
    for at in (2 * MIN, 2 * MIN + timedelta(seconds=30)):
        await tick(engine, T + at)
    await tick(engine, T + 5 * MIN)  # window T+3..T+5: good -> recovering
    assert await state(engine) == "recovering"
    await tick(engine, T + 6 * MIN)  # window T+4..T+6 holds T+5's errors again
    [incident] = await incidents(engine)
    assert incident.state == "open"
    assert [e.type for e in await events(engine, incident.id)] == ["opened", "breached_again"]


async def test_silence_keeps_the_incident_open(engine: AsyncEngine) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 2, requests=20, errors=10)
    for at in (2 * MIN, 2 * MIN + timedelta(seconds=30)):
        await tick(engine, T + at)
    for i in range(10):  # the gateway stops reporting entirely
        await tick(engine, T + 6 * MIN + i * timedelta(seconds=15))
    assert await state(engine) == "no_data"
    [incident] = await incidents(engine)
    assert incident.state == "open"
    assert [e.type for e in await events(engine, incident.id)] == ["opened", "no_data"]


async def test_a_silent_service_raises_unavailable_with_its_callers_failures(
    engine: AsyncEngine,
) -> None:
    await only(engine, "Service unavailable")
    await minutes(engine, "inventory", 0, 2, requests=20, errors=0)
    await minutes(engine, "orders", 0, 8, requests=20, errors=20)
    failed_call = await span(
        engine, "orders", T + 5 * MIN, kind=3, attributes='{"peer.service": "inventory"}'
    )
    await tick(engine, T + 3 * MIN)
    assert await state(engine, "inventory") == "ok"
    result = await tick(engine, T + 6 * MIN)  # nothing from inventory for 2 minutes
    assert result.opened == 1  # for_s is 0: silence for the whole window is enough
    [incident] = await incidents(engine)
    assert incident.service == "inventory"
    [opened] = await events(engine, incident.id)
    assert opened.sample_trace_ids == [failed_call]
    assert await state(engine, "orders") == "ok"  # orders is busy, so not "unavailable"


async def test_a_starved_service_is_not_called_unavailable(engine: AsyncEngine) -> None:
    """Regression (found on the live stack): with inventory unreachable, orders fails before
    calling payments, so payments went silent and was reported unavailable. It isn't."""
    await only(engine, "Service unavailable")
    await minutes(engine, "payments", 0, 2, requests=20, errors=0)
    for m in range(3):  # orders called payments normally, then stopped calling it
        await span(engine, "orders", T + m * MIN, kind=3, error=False,
                   attributes='{"peer.service": "payments"}')  # fmt: skip
    await tick(engine, T + 3 * MIN)
    await tick(engine, T + 6 * MIN)
    assert await incidents(engine) == []
    assert await state(engine, "payments") == "no_data"


async def test_evidence_comes_from_settled_data(engine: AsyncEngine) -> None:
    """Regression: the newest failing traces may still be missing spans, so the linked
    evidence must come from the window the rule judged, not from the last few seconds."""
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 4, requests=20, errors=10)
    settled = await span(engine, "gateway", T + 2 * MIN)
    now = T + 3 * MIN + timedelta(seconds=10)
    in_transit = await span(engine, "gateway", now - timedelta(seconds=5))  # newer than 30 s
    settle = timedelta(seconds=30)
    await evaluator.run_once(engine, now - timedelta(seconds=30), settle)
    await evaluator.run_once(engine, now, settle)
    [incident] = await incidents(engine)
    [opened] = await events(engine, incident.id)
    assert settled in opened.sample_trace_ids
    assert in_transit not in opened.sample_trace_ids


async def test_a_new_peak_is_recorded_once_it_is_clearly_worse(engine: AsyncEngine) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 2, requests=20, errors=4)  # 20%
    await minutes(engine, "gateway", 3, 6, requests=20, errors=12)  # 60%
    for at in (2 * MIN, 2 * MIN + timedelta(seconds=30), 5 * MIN):
        await tick(engine, T + at)
    [incident] = await incidents(engine)
    assert incident.peak_value == pytest.approx(0.6)
    assert [(e.type, round(e.value, 2)) for e in await events(engine, incident.id)] == [
        ("opened", 0.2),
        ("peak", 0.6),
    ]


async def test_state_survives_a_restart(clean_db: str) -> None:
    """The `for` timer lives in the database: a pass from a brand-new process continues it."""
    first = create_async_engine(clean_db)
    await only(first, "Checkout errors")
    await minutes(first, "gateway", 0, 4, requests=20, errors=10)
    await tick(first, T + 2 * MIN)
    await first.dispose()  # "restart"

    second = create_async_engine(clean_db)
    result = await tick(second, T + 2 * MIN + timedelta(seconds=30))
    await second.dispose()
    assert result.opened == 1


async def test_disabling_a_rule_closes_its_incident_with_the_reason(engine: AsyncEngine) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 4, requests=20, errors=10)
    for at in (2 * MIN, 2 * MIN + timedelta(seconds=30)):
        await tick(engine, T + at)
    await sql(engine, "UPDATE alert_rules SET enabled = false")
    result = await tick(engine, T + 3 * MIN)
    assert result.resolved == 1
    [incident] = await incidents(engine)
    *_, last = await events(engine, incident.id)
    assert (incident.state, last.type) == ("resolved", "resolved")
    assert "disabled" in last.detail
    assert await sql(engine, "SELECT * FROM alert_states") == []


async def test_only_one_evaluator_runs_at_a_time(engine: AsyncEngine) -> None:
    """Two ServScope processes on one database: the advisory lock lets one pass in."""
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": evaluator.LOCK_KEY})
        result = await tick(engine, T)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": evaluator.LOCK_KEY})
    assert result.skipped is True
    assert (await tick(engine, T)).skipped is False


async def test_concurrent_passes_never_open_duplicate_incidents(engine: AsyncEngine) -> None:
    await only(engine, "Checkout errors")
    await minutes(engine, "gateway", 0, 4, requests=20, errors=10)
    await tick(engine, T + 2 * MIN)
    at = T + 2 * MIN + timedelta(seconds=30)
    results = await asyncio.gather(*(tick(engine, at) for _ in range(5)))
    assert sum(r.opened for r in results) == 1
    assert len(await incidents(engine)) == 1


async def test_the_partial_unique_index_enforces_one_open_incident(engine: AsyncEngine) -> None:
    """The database itself refuses a second open incident for the same rule and service,
    whatever the code does; resolved ones may repeat freely."""
    insert = (
        "INSERT INTO incidents (rule_id, rule_name, kind, service, threshold, state, opened_at) "
        "VALUES (1, 'r', 'error_rate', 'gateway', 0.1, :s, now())"
    )
    await sql(engine, insert, s="resolved")
    await sql(engine, insert, s="resolved")
    await sql(engine, insert, s="open")
    with pytest.raises(Exception, match="ux_incidents_one_open"):
        await sql(engine, insert, s="open")


async def test_silence_has_no_peak(engine: AsyncEngine) -> None:
    """Regression (seen in the dashboard's history): an unavailable incident's "peak" showed
    the request count after recovery ("47 requests"), which says nothing about the outage."""
    await only(engine, "Service unavailable")
    await minutes(engine, "inventory", 0, 1, requests=20, errors=0)
    await minutes(engine, "inventory", 5, 9, requests=40, errors=0)  # back, and busy
    for at in (4 * MIN, 6 * MIN, 7 * MIN, 8 * MIN):
        await tick(engine, T + at)
    [incident] = await incidents(engine)
    assert incident.state == "resolved"
    assert incident.peak_value == 0
    assert [e.type for e in await events(engine, incident.id)] == ["opened", "resolved"]
