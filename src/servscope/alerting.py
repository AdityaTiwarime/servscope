"""Alert rules: what each kind measures, and the state machine that turns measurements into
incidents. Pure functions only; `evaluator.py` does the database work around them.

Every rule is checked for every service it covers, every 15 seconds:

    ok ──breach──▶ pending ──breach for `for_s`──▶ firing        (incident opened)
     ▲                │                              │  ▲
     │             clear                          clear │ breach again
     │                ▼                              ▼  │
     └──────────── ok ◀──clear for `recover_s`── recovering      (incident resolved)

    no data (nothing measured) from any state ──▶ no_data. An open incident stays open:
    silence is not proof that the problem went away.

Why `for_s` and `recover_s`: one bad minute should not page anyone, and a value hovering
around the threshold should not open and close an incident every 15 seconds ("flapping").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from .latency import percentile

Kind = Literal["error_rate", "p95_latency", "unavailable", "failure_rise"]

# Below this many requests in the window an error rate is not judged at all: 1 failure in 3
# requests is 33%, but it says nothing.
MIN_REQUESTS = 10
# "Failures rising" compares errors per minute in the window with the half hour before it.
# The baseline never counts as less than this, so going from 0 to 1 error isn't "infinite".
BASELINE = timedelta(minutes=30)
BASELINE_FLOOR_PER_MIN = 0.5
MIN_RISE_ERRORS = 5
# A new peak is written to the incident timeline only when it is this much worse than the
# previous one, so a slowly growing value adds a few events, not one every 15 seconds.
PEAK_STEP = 1.25


class State(StrEnum):
    OK = "ok"
    PENDING = "pending"
    FIRING = "firing"
    RECOVERING = "recovering"
    NO_DATA = "no_data"


class Action(StrEnum):
    OPEN = "opened"
    RESOLVE = "resolved"
    BREACHED_AGAIN = "breached_again"
    NO_DATA = "no_data"
    DATA_BACK = "data_back"


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    kind: Kind
    service: str | None
    threshold: float
    window_s: int
    for_s: int
    recover_s: int


@dataclass(frozen=True)
class Totals:
    requests: int
    errors: int
    buckets: Sequence[int]


@dataclass(frozen=True)
class Calls:
    """Calls OTHER services attempted to this one (their client spans), whether or not any
    arrived. `in_window`: during the rule's window; `before`: in the half hour before it."""

    in_window: int
    before: int


@dataclass(frozen=True)
class Observation:
    value: float | None  # in the rule's unit; None when nothing could be measured
    breaching: bool | None  # None: no data


NO_DATA = Observation(None, None)


def observe(
    rule: Rule, window: Totals | None, baseline: Totals | None = None, calls: Calls | None = None
) -> Observation:
    """Measure one service for one rule. `window` covers the rule's window; `baseline` the
    half hour before it (only "failures rising" uses it); `calls` the calls other services
    attempted to it (only "unavailable" uses it)."""
    requests = window.requests if window else 0
    if rule.kind == "unavailable":
        return _unavailable(requests, calls)
    if window is None or requests == 0:
        return NO_DATA
    if rule.kind == "error_rate":
        rate = window.errors / requests
        return Observation(rate, requests >= MIN_REQUESTS and rate > rule.threshold)
    if rule.kind == "p95_latency":
        p95 = percentile(window.buckets, 0.95)
        if p95 is None:  # too few requests for a meaningful p95: measured, not breaching
            return Observation(None, False)
        return Observation(p95.ms, p95.ms > rule.threshold)
    # failure_rise: errors per minute now, as a multiple of errors per minute before.
    now_per_min = window.errors / (rule.window_s / 60)
    before = baseline.errors if baseline else 0
    before_per_min = max(before / (BASELINE.total_seconds() / 60), BASELINE_FLOOR_PER_MIN)
    ratio = now_per_min / before_per_min
    return Observation(ratio, window.errors >= MIN_RISE_ERRORS and ratio > rule.threshold)


def _unavailable(requests: int, calls: Calls | None) -> Observation:
    """Silence alone can't tell "down" from "nobody called". When inventory is unreachable,
    orders fails before it ever calls payments, so payments goes silent too, while being
    perfectly fine. The callers' attempted calls tell the cases apart:

    - requests arrived: available.
    - callers tried and nothing arrived: unreachable or down. Alert.
    - callers used to call it and stopped: starved. Its silence says nothing about it, so
      that is "no data", never an alert and never a recovery.
    - no caller has called it at all (an entry point like the gateway): its silence is the
      only signal there is. Alert.
    """
    if requests:
        return Observation(float(requests), False)
    calls = calls or Calls(0, 0)
    if calls.in_window == 0 and calls.before > 0:
        return NO_DATA
    return Observation(0.0, True)


@dataclass(frozen=True)
class Current:
    state: State
    since: datetime


def step(
    prev: Current | None, obs: Observation, rule: Rule, now: datetime, has_incident: bool
) -> tuple[Current, list[Action]]:
    """One evaluation: the new state and what to record. `has_incident` says whether an
    incident is open for this rule and service right now."""
    prev = prev or Current(State.OK, now)

    if obs.breaching is None:
        if prev.state == State.NO_DATA:
            return prev, []
        return Current(State.NO_DATA, now), [Action.NO_DATA] if has_incident else []

    actions: list[Action] = []
    if prev.state == State.NO_DATA:
        if has_incident:
            # Data is back while an incident is open: carry on from "firing" so the incident
            # is resolved only after a full recover_s of good data.
            actions.append(Action.DATA_BACK)
            prev = Current(State.FIRING, now)
        else:
            prev = Current(State.OK, now)

    since = now - prev.since
    if obs.breaching:
        if prev.state == State.OK:
            if rule.for_s == 0:
                return Current(State.FIRING, now), [*actions, Action.OPEN]
            return Current(State.PENDING, now), actions
        if prev.state == State.PENDING:
            if since >= timedelta(seconds=rule.for_s):
                return Current(State.FIRING, now), [*actions, Action.OPEN]
            return prev, actions
        if prev.state == State.RECOVERING:
            return Current(State.FIRING, now), [*actions, Action.BREACHED_AGAIN]
        return prev, actions  # firing stays firing

    # Not breaching.
    if prev.state in (State.OK, State.PENDING):
        return (prev if prev.state == State.OK else Current(State.OK, now)), actions
    if prev.state == State.FIRING:
        if rule.recover_s == 0:
            return Current(State.OK, now), [*actions, Action.RESOLVE]
        return Current(State.RECOVERING, now), actions
    # recovering
    if since >= timedelta(seconds=rule.recover_s):
        return Current(State.OK, now), [*actions, Action.RESOLVE]
    return prev, actions


def worse(kind: Kind, value: float, than: float | None) -> bool:
    """Is `value` a new peak worth recording? Unavailable has no peak: silent is silent."""
    if kind == "unavailable":
        return False
    return than is None or value >= than * PEAK_STEP


def describe(rule: Rule) -> str:
    """The rule as a sentence, for the dashboard and incident titles."""
    window = _duration(rule.window_s)
    if rule.kind == "error_rate":
        what = f"error rate above {rule.threshold:.0%} over {window}"
    elif rule.kind == "p95_latency":
        what = f"p95 latency above {rule.threshold:g} ms over {window}"
    elif rule.kind == "unavailable":
        what = f"no requests for {window}"
    else:
        what = f"errors per minute above {rule.threshold:g}x the previous 30 min, over {window}"
    hold = f", for {_duration(rule.for_s)}" if rule.for_s else ""
    return what + hold


def _duration(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} min"
    return f"{seconds} s"
