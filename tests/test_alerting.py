"""Milestone 5: the alert rules' measurements and state machine, as pure functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from servscope.alerting import (
    MIN_REQUESTS,
    NO_DATA,
    Action,
    Calls,
    Current,
    Observation,
    Rule,
    State,
    Totals,
    describe,
    observe,
    step,
    worse,
)
from servscope.latency import N_BUCKETS, bucket_index

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TICK = timedelta(seconds=15)


def rule(kind: str = "error_rate", threshold: float = 0.1, **kw: int) -> Rule:
    params = {"window_s": 120, "for_s": 30, "recover_s": 60, **kw}
    return Rule(1, "r", kind, "gateway", threshold, **params)  # type: ignore[arg-type]


def totals(requests: int, errors: int = 0, ms: float = 20) -> Totals:
    buckets = [0] * N_BUCKETS
    buckets[bucket_index(int(ms * 1_000_000))] = requests
    return Totals(requests, errors, buckets)


BAD, GOOD = Observation(0.5, True), Observation(0.0, False)


# --- what each kind measures -----------------------------------------------------------


def test_error_rate_needs_enough_requests_to_judge() -> None:
    few = observe(rule(), totals(MIN_REQUESTS - 1, errors=5))
    assert few.value == pytest.approx(5 / 9) and few.breaching is False
    assert observe(rule(), totals(100, errors=11)).breaching is True
    assert observe(rule(), totals(100, errors=10)).breaching is False  # strictly above


def test_no_requests_is_no_data_not_healthy() -> None:
    assert observe(rule(), None) == NO_DATA
    assert observe(rule(), totals(0)) == NO_DATA
    assert observe(rule("p95_latency", 1000), None) == NO_DATA


def test_p95_is_judged_only_with_enough_samples() -> None:
    slow = rule("p95_latency", 1000)
    too_few = observe(slow, totals(5, ms=3000))
    assert too_few == Observation(None, False)  # measured, but too few for a p95
    breach = observe(slow, totals(50, ms=3000))
    assert breach.breaching is True and breach.value is not None and breach.value > 1000
    assert observe(slow, totals(50, ms=40)).breaching is False


def test_unavailable_tells_down_from_starved() -> None:
    gone = rule("unavailable", 0, for_s=0)
    assert observe(gone, totals(3)) == Observation(3.0, False)  # requests arrived
    # Callers tried and nothing arrived: unreachable or down.
    assert observe(gone, None, calls=Calls(in_window=40, before=200)) == Observation(0.0, True)
    # Callers used to call it and stopped: starved, not broken. Silence proves nothing.
    assert observe(gone, None, calls=Calls(in_window=0, before=200)) == NO_DATA
    # Nobody calls it at all (an entry point): silence is the only signal, so it counts.
    assert observe(gone, None, calls=None) == Observation(0.0, True)


def test_failure_rise_compares_with_the_half_hour_before() -> None:
    rising = rule("failure_rise", 3, window_s=300)
    # 10 errors in 5 min = 2/min, against 30 in the previous 30 min = 1/min: twice, not 3x.
    assert observe(rising, totals(200, 10), totals(2000, 30)).breaching is False
    # 20 errors in 5 min = 4/min: 4x the baseline.
    hot = observe(rising, totals(200, 20), totals(2000, 30))
    assert hot.value == pytest.approx(4.0) and hot.breaching is True
    # From a clean baseline the floor (0.5/min) applies, so 3 errors are no "infinite" rise,
    # and fewer than 5 errors never count.
    assert observe(rising, totals(200, 3), None).breaching is False
    assert observe(rising, totals(200, 8), None).breaching is True  # 1.6/min vs 0.5/min


# --- the state machine -----------------------------------------------------------------


def run(r: Rule, observations: list[Observation]) -> list[tuple[State, list[Action]]]:
    """Feed one observation per 15 s tick; returns the state and actions after each."""
    current: Current | None = None
    has_incident = False
    out = []
    for i, obs in enumerate(observations):
        current, actions = step(current, obs, r, T0 + i * TICK, has_incident)
        if Action.OPEN in actions:
            has_incident = True
        if Action.RESOLVE in actions:
            has_incident = False
        out.append((current.state, actions))
    return out


def test_full_lifecycle_with_for_and_recover() -> None:
    #      t=0   15    30    45    60    75    90   105   120   135
    obs = [BAD, BAD, BAD, GOOD, BAD, GOOD, GOOD, GOOD, GOOD, GOOD]
    states = run(rule(for_s=30, recover_s=60), obs)
    assert [s for s, _ in states] == [
        State.PENDING,  # breaching, waiting for 30 s
        State.PENDING,
        State.FIRING,  # 30 s of breach: incident opened
        State.RECOVERING,
        State.FIRING,  # breached again while recovering: same incident
        State.RECOVERING,  # recover clock starts again at t=75
        State.RECOVERING,
        State.RECOVERING,
        State.RECOVERING,
        State.OK,  # t=135: 60 s of good data since t=75, resolved
    ]
    actions = [a for _, acts in states for a in acts]
    assert actions == [Action.OPEN, Action.BREACHED_AGAIN, Action.RESOLVE]


def test_flapping_around_the_threshold_never_opens_an_incident() -> None:
    states = run(rule(for_s=30), [BAD, GOOD] * 10)
    assert all(not acts for _, acts in states)
    assert {s for s, _ in states} == {State.PENDING, State.OK}


def test_silence_never_resolves_an_open_incident() -> None:
    obs = [BAD, BAD, BAD, NO_DATA, NO_DATA, NO_DATA, NO_DATA, NO_DATA, NO_DATA]
    states = run(rule(for_s=30, recover_s=30), obs)
    assert states[2] == (State.FIRING, [Action.OPEN])
    assert states[3] == (State.NO_DATA, [Action.NO_DATA])  # noted once on the timeline...
    assert all(s == (State.NO_DATA, []) for s in states[4:])  # ...not every 15 s
    assert not any(Action.RESOLVE in acts for _, acts in states)


def test_after_silence_the_incident_needs_a_full_recovery_to_resolve() -> None:
    obs = [BAD, BAD, BAD, NO_DATA, GOOD, GOOD, GOOD]
    states = run(rule(for_s=30, recover_s=30), obs)
    assert states[4] == (State.RECOVERING, [Action.DATA_BACK])
    assert states[5] == (State.RECOVERING, [])
    assert states[6] == (State.OK, [Action.RESOLVE])  # 30 s of good data after it came back


def test_silence_without_an_incident_records_nothing() -> None:
    states = run(rule(), [GOOD, NO_DATA, NO_DATA, GOOD])
    assert [s for s, _ in states] == [State.OK, State.NO_DATA, State.NO_DATA, State.OK]
    assert all(not acts for _, acts in states)


def test_zero_for_and_recover_act_immediately() -> None:
    states = run(rule(for_s=0, recover_s=0), [BAD, GOOD])
    assert states == [(State.FIRING, [Action.OPEN]), (State.OK, [Action.RESOLVE])]


def test_a_measured_but_unjudgeable_value_does_not_breach() -> None:
    # p95 with too few samples: data exists, so it is not "no data"; it just isn't a breach.
    states = run(rule("p95_latency", 1000, for_s=0), [Observation(None, False)])
    assert states == [(State.OK, [])]


# --- wording and peaks -----------------------------------------------------------------


def test_rules_read_as_sentences() -> None:
    assert describe(rule()) == "error rate above 10% over 2 min, for 30 s"
    assert describe(rule("p95_latency", 1000, for_s=0)) == "p95 latency above 1000 ms over 2 min"
    assert describe(rule("unavailable", 0, for_s=0)) == "no requests for 2 min"


def test_a_peak_is_recorded_only_when_clearly_worse() -> None:
    assert worse("error_rate", 0.5, None)
    assert not worse("error_rate", 0.6, 0.5)
    assert worse("error_rate", 0.625, 0.5)
    assert not worse("unavailable", 0.0, None)
