"""Unit tests for latency percentiles, secret scrubbing and trace analysis."""

import pytest

from servscope.latency import (
    BUCKET_EDGES_MS,
    MIN_SAMPLES,
    N_BUCKETS,
    add_buckets,
    bucket_index,
    empty_buckets,
    percentile,
)
from servscope.scrub import REDACTED, scrub_attributes, scrub_text
from servscope.trace_analysis import analyse

MS = 1_000_000


# --- latency ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ms", "index"),
    [(0, 0), (5, 0), (5.001, 1), (10, 1), (99, 4), (100, 4), (10_000, 10), (60_000, 11)],
)
def test_bucket_edges_are_inclusive_upper_bounds(ms: float, index: int) -> None:
    assert bucket_index(int(ms * MS)) == index


def test_too_few_samples_gives_no_percentile() -> None:
    buckets = empty_buckets()
    buckets[3] = MIN_SAMPLES - 1
    assert percentile(buckets, 0.99) is None


def test_percentile_is_interpolated_inside_its_bucket() -> None:
    # 100 requests, all between 50 and 100 ms: p50 lands in the middle of that bucket.
    buckets = empty_buckets()
    buckets[4] = 100
    p50 = percentile(buckets, 0.5)
    assert p50 is not None and p50.ms == pytest.approx(75) and not p50.at_least


def test_p99_finds_the_slow_tail() -> None:
    buckets = empty_buckets()
    buckets[2] = 980  # fast: 10-25 ms
    buckets[8] = 20  # slow: 1-2.5 s
    p50, p99 = percentile(buckets, 0.5), percentile(buckets, 0.99)
    assert p50 is not None and 10 < p50.ms <= 25
    assert p99 is not None and 1000 < p99.ms <= 2500


def test_percentile_in_the_open_top_bucket_is_a_lower_bound() -> None:
    buckets = empty_buckets()
    buckets[-1] = 50
    p = percentile(buckets, 0.95)
    assert p is not None and p.ms == BUCKET_EDGES_MS[-1] and p.at_least


def test_bucket_lists_add_elementwise() -> None:
    a, b = empty_buckets(), empty_buckets()
    a[0], b[0], b[-1] = 2, 3, 1
    assert add_buckets(a, b)[0] == 5 and add_buckets(a, b)[-1] == 1
    assert len(add_buckets(a, b)) == N_BUCKETS


# --- scrubbing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("charge failed for card 4111 1111 1111 1111", "4111 1111 1111 1111"),
        ("card=4111-1111-1111-1111 declined", "4111-1111-1111-1111"),
        ("login failed password=hunter2 user=asha", "hunter2"),
        ("retrying with token: abc.def.ghi", "abc.def.ghi"),
        ("api_key=sk_live_123", "sk_live_123"),
    ],
)
def test_secrets_in_text_are_redacted(text: str, leaked: str) -> None:
    out = scrub_text(text)
    assert leaked not in out and REDACTED in out


def test_ordinary_numbers_survive() -> None:
    text = "order 7aa980e4 total 58900 paise, 3 items, took 1250 ms"
    assert scrub_text(text) == text


def test_secret_attribute_keys_are_redacted_at_any_depth() -> None:
    attrs = {
        "http.request.header.authorization": "Bearer xyz",
        "user": {"name": "asha", "password": "p"},
        "card_number": "4111111111111111",
        "cart.items": 3,
    }
    out = scrub_attributes(attrs)
    assert out["http.request.header.authorization"] == REDACTED
    assert out["user"] == {"name": "asha", "password": REDACTED}
    assert out["card_number"] == REDACTED
    assert out["cart.items"] == 3


# --- trace analysis --------------------------------------------------------------------


def checkout_trace() -> list[tuple[str, str | None, int, int]]:
    #  gateway  0 ........................................ 100
    #  orders     5 ................................... 95
    #  inventory    10 .... 20
    #  payments                25 ................ 90     <- the slow part
    return [
        ("gw", None, 0, 100 * MS),
        ("od", "gw", 5 * MS, 90 * MS),
        ("inv", "od", 10 * MS, 10 * MS),
        ("pay", "od", 25 * MS, 65 * MS),
    ]


def test_depths_and_own_time() -> None:
    a = analyse(checkout_trace())
    assert [a.nodes[s].depth for s in ("gw", "od", "inv", "pay")] == [0, 1, 2, 2]
    assert a.nodes["gw"].self_ns == 10 * MS  # 100 minus the 90 orders took
    assert a.nodes["od"].self_ns == 15 * MS  # 90 minus 10 (inventory) minus 65 (payments)
    assert a.nodes["pay"].self_ns == 65 * MS


def test_critical_path_follows_what_finished_last() -> None:
    a = analyse(checkout_trace())
    assert a.critical_path == ["gw", "od", "pay"]
    assert not a.nodes["inv"].on_critical_path


def test_slowest_span_is_the_one_with_most_own_time() -> None:
    assert analyse(checkout_trace()).slowest_span_id == "pay"


def test_parallel_children_are_not_double_subtracted() -> None:
    spans = [("p", None, 0, 100 * MS), ("a", "p", 10 * MS, 50 * MS), ("b", "p", 20 * MS, 50 * MS)]
    # a and b overlap: together they cover 10..70 = 60 ms, so the parent's own time is 40.
    assert analyse(spans).nodes["p"].self_ns == 40 * MS


def test_missing_parent_becomes_a_root_instead_of_disappearing() -> None:
    spans = [("gw", None, 0, 10 * MS), ("orphan", "never-arrived", 2 * MS, 1 * MS)]
    a = analyse(spans)
    assert {r.span_id for r in a.roots} == {"gw", "orphan"}


def test_child_starting_before_parent_is_flagged_as_clock_skew() -> None:
    spans = [("p", None, 10 * MS, 20 * MS), ("c", "p", 5 * MS, 10 * MS)]
    assert analyse(spans).nodes["c"].starts_before_parent


def test_a_very_deep_trace_does_not_overflow() -> None:
    spans: list[tuple[str, str | None, int, int]] = [("s0", None, 0, 10_000)]
    spans += [(f"s{i}", f"s{i - 1}", i, 10_000 - 2 * i) for i in range(1, 3000)]
    a = analyse(spans)
    assert a.nodes["s2999"].depth == 2999


def test_empty_trace() -> None:
    a = analyse([])
    assert a.roots == [] and a.critical_path == [] and a.slowest_span_id is None
