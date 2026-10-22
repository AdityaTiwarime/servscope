"""Runs the alert rules against the stored metrics and keeps incidents up to date.

One pass every `alert_interval_seconds`, all in ONE transaction: states, incidents and their
events change together or not at all. If ServScope crashes halfway, nothing half-done is left
behind, and the next pass simply looks again.

Only one evaluator may run at a time. If two ServScope processes share a database, both
start passes, but `pg_try_advisory_xact_lock` lets only one in; the other skips that round.
The lock is released when the transaction ends, even if the process dies.

Each pass looks at data up to `alert_settle_seconds` ago, not up to now: spans spend a few
seconds in the services' and the Collector's batches, and the newest seconds would always
look emptier than they really were. Rollups are per minute, so a window is rounded to whole
minutes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .alerting import (
    BASELINE,
    Action,
    Calls,
    Current,
    Rule,
    State,
    Totals,
    describe,
    observe,
    step,
    worse,
)
from .config import Settings
from .query import Window, service_totals
from .stats import Stats

log = logging.getLogger("servscope.alerts")

LOCK_KEY = 0x5E5C0FE  # any fixed number; it names "the alert evaluator" lock
KNOWN_FOR = timedelta(hours=24)  # a service seen this recently is watched by "every service" rules
SAMPLES = 5

_SAMPLE_ERRORS = text("""
    SELECT trace_id FROM spans
    WHERE service = :service AND kind = 2 AND status_code = 2
      AND start_time >= :start AND start_time < :end
    GROUP BY trace_id ORDER BY max(start_time) DESC LIMIT :n
""")
_SAMPLE_SLOWEST = text("""
    SELECT trace_id FROM spans
    WHERE service = :service AND kind = 2 AND start_time >= :start AND start_time < :end
    ORDER BY duration_ns DESC LIMIT :n
""")
# A silent service has no spans of its own; the evidence is its callers' failed calls to it.
_SAMPLE_FAILED_CALLS = text("""
    SELECT trace_id FROM spans
    WHERE kind = 3 AND status_code = 2 AND attributes->>'peer.service' = :service
      AND start_time >= :start AND start_time < :end
    GROUP BY trace_id ORDER BY max(start_time) DESC LIMIT :n
""")
# Calls attempted TO each service (client spans in other services), in the window and in the
# half hour before it. Used to tell an unreachable service from one nobody is calling.
_ATTEMPTED_CALLS = text("""
    SELECT attributes->>'peer.service' AS callee,
           count(*) FILTER (WHERE start_time >= :start) AS in_window,
           count(*) FILTER (WHERE start_time < :start) AS before
    FROM spans
    WHERE kind = 3 AND start_time >= :since AND start_time < :end AND attributes ? 'peer.service'
    GROUP BY 1
""")
_OPEN_INCIDENTS = text("""
    SELECT i.id, i.rule_id, i.service, i.peak_value,
           (SELECT e.value FROM incident_events e
            WHERE e.incident_id = i.id AND e.type IN ('opened', 'peak') AND e.value IS NOT NULL
            ORDER BY e.time DESC, e.id DESC LIMIT 1) AS recorded
    FROM incidents i WHERE i.state = 'open'
""")
_UPSERT_STATE = text("""
    INSERT INTO alert_states (rule_id, service, state, since, value, evaluated_at)
    VALUES (:rule_id, :service, :state, :since, :value, :now)
    ON CONFLICT (rule_id, service) DO UPDATE SET
        state = excluded.state, since = excluded.since,
        value = excluded.value, evaluated_at = excluded.evaluated_at
""")
_OPEN = text("""
    INSERT INTO incidents (rule_id, rule_name, kind, service, threshold, state, opened_at,
                           peak_value)
    VALUES (:rule_id, :rule_name, :kind, :service, :threshold, 'open', :now, :value)
    RETURNING id
""")
_RESOLVE = text("UPDATE incidents SET state = 'resolved', resolved_at = :now WHERE id = :id")
_EVENT = text("""
    INSERT INTO incident_events (incident_id, time, type, value, detail, sample_trace_ids)
    VALUES (:incident_id, :now, :type, :value, :detail, :samples)
""")


@dataclass
class PassResult:
    evaluated: int = 0
    opened: int = 0
    resolved: int = 0
    skipped: bool = False  # another evaluator holds the lock


@dataclass
class OpenIncident:
    id: int
    peak: float | None  # worst value seen (kept exactly)
    recorded: float | None  # value of the last "opened"/"peak" event on the timeline


async def add_event(
    conn: AsyncConnection,
    incident_id: int,
    now: datetime,
    type_: str,
    value: float | None = None,
    detail: str = "",
    samples: list[str] | None = None,
) -> None:
    params = {
        "incident_id": incident_id,
        "now": now,
        "type": type_,
        "value": value,
        "detail": detail,
        "samples": samples or [],
    }
    await conn.execute(_EVENT, params)


async def resolve(conn: AsyncConnection, incident_id: int, now: datetime, detail: str) -> None:
    await conn.execute(_RESOLVE, {"id": incident_id, "now": now})
    await add_event(conn, incident_id, now, "resolved", detail=detail)


async def sample_traces(
    conn: AsyncConnection, rule: Rule, service: str, start: datetime, end: datetime
) -> list[str]:
    query = {
        "error_rate": _SAMPLE_ERRORS,
        "failure_rise": _SAMPLE_ERRORS,
        "p95_latency": _SAMPLE_SLOWEST,
        "unavailable": _SAMPLE_FAILED_CALLS,
    }[rule.kind]
    params = {"service": service, "start": start, "end": end, "n": SAMPLES}
    ids = [r.trace_id for r in await conn.execute(query, params)]
    return list(dict.fromkeys(ids))  # unique, order kept


async def run_once(engine: AsyncEngine, now: datetime, settle: timedelta) -> PassResult:
    async with engine.begin() as conn:
        locked = await conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        if not locked.scalar_one():
            return PassResult(skipped=True)
        return await _evaluate(conn, now, settle)


async def _evaluate(conn: AsyncConnection, now: datetime, settle: timedelta) -> PassResult:
    result = PassResult()
    end = now - settle
    rules = [
        Rule(r.id, r.name, r.kind, r.service, r.threshold, r.window_s, r.for_s, r.recover_s)
        for r in await conn.execute(
            text(
                "SELECT id, name, kind, service, threshold, window_s, for_s, recover_s "
                "FROM alert_rules WHERE enabled ORDER BY id"
            )
        )
    ]
    known = [
        r.service
        for r in await conn.execute(
            text("SELECT DISTINCT service FROM metric_rollups WHERE minute >= :since"),
            {"since": now - KNOWN_FOR},
        )
    ]
    states = {
        (r.rule_id, r.service): Current(State(r.state), r.since)
        for r in await conn.execute(text("SELECT rule_id, service, state, since FROM alert_states"))
    }
    open_incidents = {
        (r.rule_id, r.service): OpenIncident(r.id, r.peak_value, r.recorded)
        for r in await conn.execute(_OPEN_INCIDENTS)
    }

    cache: dict[tuple[datetime, datetime], dict[str, Totals]] = {}

    async def totals(start: datetime, stop: datetime) -> dict[str, Totals]:
        if (start, stop) not in cache:
            raw = await service_totals(conn, Window(start, stop))
            cache[start, stop] = {
                name: Totals(t["requests"], t["errors"], t["buckets"]) for name, t in raw.items()
            }
        return cache[start, stop]

    calls_cache: dict[tuple[datetime, datetime], dict[str, Calls]] = {}

    async def attempted(start: datetime, stop: datetime) -> dict[str, Calls]:
        if (start, stop) not in calls_cache:
            params = {"since": start - BASELINE, "start": start, "end": stop}
            calls_cache[start, stop] = {
                r.callee: Calls(r.in_window, r.before)
                for r in await conn.execute(_ATTEMPTED_CALLS, params)
            }
        return calls_cache[start, stop]

    seen: set[tuple[int, str]] = set()
    for rule in rules:
        start = end - timedelta(seconds=rule.window_s)
        in_window = await totals(start, end)
        before = await totals(start - BASELINE, start) if rule.kind == "failure_rise" else {}
        calls = await attempted(start, end) if rule.kind == "unavailable" else {}
        for service in [rule.service] if rule.service else known:
            key = (rule.id, service)
            seen.add(key)
            obs = observe(rule, in_window.get(service), before.get(service), calls.get(service))
            incident = open_incidents.get(key)
            new, actions = step(states.get(key), obs, rule, now, incident is not None)
            await conn.execute(
                _UPSERT_STATE,
                {
                    "rule_id": rule.id,
                    "service": service,
                    "state": new.state.value,
                    "since": new.since,
                    "value": obs.value,
                    "now": now,
                },
            )
            result.evaluated += 1
            for action in actions:
                incident = await _apply(
                    conn, action, rule, service, obs.value, now, (start, end), incident
                )
                if action == Action.OPEN:
                    result.opened += 1
                elif action == Action.RESOLVE:
                    result.resolved += 1
            if incident is None:
                open_incidents.pop(key, None)
                continue
            open_incidents[key] = incident
            # Silence has no "peak": an unavailable service's value only tells how many
            # requests came back once it recovered, which is not how bad the outage was.
            if obs.value is not None and Action.OPEN not in actions and rule.kind != "unavailable":
                await _track_peak(conn, rule, incident, obs.value, now)

    # A (rule, service) pair not evaluated this time: the rule was disabled, changed to cover
    # other services, or the service has been silent for over a day. Its open incident can
    # never resolve by itself any more, so it is closed with the reason.
    for key in set(states) - seen:
        await conn.execute(
            text("DELETE FROM alert_states WHERE rule_id = :r AND service = :s"),
            {"r": key[0], "s": key[1]},
        )
        if key in open_incidents:
            await resolve(
                conn,
                open_incidents[key].id,
                now,
                "the rule was disabled or no longer covers this service",
            )
            result.resolved += 1
    return result


async def _apply(
    conn: AsyncConnection,
    action: Action,
    rule: Rule,
    service: str,
    value: float | None,
    now: datetime,
    window: tuple[datetime, datetime],
    incident: OpenIncident | None,
) -> OpenIncident | None:
    """Record one action; returns the incident that is open afterwards (if any).

    Sample traces come from the same settled window the rule judged, not from "up to now":
    the newest traces may still be missing spans that are on their way."""
    if action == Action.OPEN:
        params = {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "kind": rule.kind,
            "service": service,
            "threshold": rule.threshold,
            "now": now,
            "value": value,
        }
        incident_id = (await conn.execute(_OPEN, params)).scalar_one()
        samples = await sample_traces(conn, rule, service, *window)
        await add_event(conn, incident_id, now, "opened", value, describe(rule), samples)
        log.warning("incident %s opened: %s on %s", incident_id, rule.name, service)
        return OpenIncident(incident_id, value, value)
    if incident is None:  # nothing to attach the other actions to
        return None
    if action == Action.RESOLVE:
        await resolve(conn, incident.id, now, f"back within the threshold for {rule.recover_s} s")
        log.info("incident %s resolved: %s on %s", incident.id, rule.name, service)
        return None
    if action == Action.BREACHED_AGAIN:
        samples = await sample_traces(conn, rule, service, *window)
        detail = "breached again while recovering; still the same incident"
        await add_event(conn, incident.id, now, "breached_again", value, detail, samples)
    elif action == Action.NO_DATA:
        detail = f"no data from {service}; the incident stays open until data shows recovery"
        await add_event(conn, incident.id, now, "no_data", None, detail)
    elif action == Action.DATA_BACK:
        await add_event(conn, incident.id, now, "data_back", value, f"data from {service} again")
    return incident


async def _track_peak(
    conn: AsyncConnection, rule: Rule, incident: OpenIncident, value: float, now: datetime
) -> None:
    """Keep the exact worst value on the incident, but add a "peak" event to the timeline only
    when it is clearly worse (PEAK_STEP) than the last one recorded there. Comparing with the
    last RECORDED value, not the last seen one, matters: a value creeping up 5% per pass
    would otherwise never count as a big enough jump, even after doubling."""
    if incident.peak is None or value > incident.peak:
        await conn.execute(
            text("UPDATE incidents SET peak_value = :v WHERE id = :id"),
            {"v": value, "id": incident.id},
        )
        incident.peak = value
    if worse(rule.kind, value, incident.recorded):
        await add_event(conn, incident.id, now, "peak", value, "new peak")
        incident.recorded = value


async def run_forever(engine: AsyncEngine, settings: Settings, stats: Stats) -> None:
    settle = timedelta(seconds=settings.alert_settle_seconds)
    while True:
        try:
            result = await run_once(engine, datetime.now().astimezone(), settle)
            stats.mark("alerts_ok")
            if not result.skipped:
                stats.add("servscope_alert_evaluations_total")
                stats.add("servscope_incidents_opened_total", result.opened)
                stats.add("servscope_incidents_resolved_total", result.resolved)
        except Exception:  # database down etc.: count it, keep the loop alive, try again
            stats.add("servscope_alert_evaluation_failures_total")
            stats.mark("alerts_failed")
            log.exception("alert evaluation failed; will try again")
        await asyncio.sleep(settings.alert_interval_seconds)


def as_dict(rule: Rule) -> dict[str, Any]:
    return {**rule.__dict__, "description": describe(rule)}
