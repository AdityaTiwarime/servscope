"""Latency histograms: how p50/p95/p99 are computed without keeping every duration.

Each minute rollup stores a count per bucket. To get a percentile over a window, the bucket
counts are added up and the percentile is located inside one bucket, assuming durations are
spread evenly within it. So the answer is accurate to the width of that bucket, and the
dashboard says so. Any percentile above the last finite edge is reported as that edge,
marked as a lower bound.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

# Upper edges in milliseconds; the last bucket is everything above 10 s.
BUCKET_EDGES_MS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
N_BUCKETS = len(BUCKET_EDGES_MS) + 1
MIN_SAMPLES = 20  # fewer requests than this and a p99 means nothing, so we don't report one


def bucket_index(duration_ns: int) -> int:
    """Bucket i holds durations in (edge[i-1], edge[i]]; the last holds everything above."""
    return bisect_left(BUCKET_EDGES_MS, duration_ns / 1_000_000)


def empty_buckets() -> list[int]:
    return [0] * N_BUCKETS


def add_buckets(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return [x + y for x, y in zip(a, b, strict=True)]


@dataclass(frozen=True)
class Estimate:
    ms: float
    at_least: bool = False  # True when the value fell in the open-ended top bucket


def percentile(buckets: Sequence[int], q: float) -> Estimate | None:
    """Estimate the q-th percentile (0 < q < 1). None when there are too few samples."""
    if not 0 < q < 1:
        raise ValueError("q must be between 0 and 1")
    total = sum(buckets)
    if total < MIN_SAMPLES:
        return None
    rank = q * total  # the position we're looking for, counting from the fastest request
    seen = 0
    for i, count in enumerate(buckets):
        if count and seen + count >= rank:
            if i == len(BUCKET_EDGES_MS):
                return Estimate(BUCKET_EDGES_MS[-1], at_least=True)
            low = BUCKET_EDGES_MS[i - 1] if i else 0.0
            high = BUCKET_EDGES_MS[i]
            return Estimate(low + (high - low) * (rank - seen) / count)
        seen += count
    raise AssertionError("unreachable: rank never exceeds total")
