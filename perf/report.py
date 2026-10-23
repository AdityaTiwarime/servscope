"""Turns benchmark result files into Markdown tables, so the numbers in docs/performance.md
are copied by a program, not by hand.

Each cell is the median of the runs at that level, with the range (lowest to highest run)
underneath where it matters, so run-to-run noise is visible instead of hidden.

    python perf/report.py perf/results/ingest-baseline.json [more files...]
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any


def med(runs: list[dict[str, Any]], key: str) -> float:
    values = [r[key] for r in runs if not (isinstance(r[key], float) and math.isnan(r[key]))]
    return statistics.median(values) if values else float("nan")


def span(runs: list[dict[str, Any]], key: str, fmt: str) -> str:
    values = [r[key] for r in runs]
    return f"{format(min(values), fmt)}–{format(max(values), fmt)}"


def ingest_table(data: dict[str, Any]) -> str:
    lines = [
        "| Concurrent exports | Accepted spans/s | Refused | Export p50 / p95 / p99 (ms) | ServScope CPU | ServScope memory | PostgreSQL CPU |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for level in data["levels"]:
        runs = [r for r in level["runs"] if r["valid"]]
        lines.append(
            f"| {level['users']} "
            f"| {med(runs, 'accepted_spans_per_s'):,.0f} ({span(runs, 'accepted_spans_per_s', ',.0f')}) "
            f"| {med(runs, 'refused_share'):.0%} "
            f"| {med(runs, 'p50_ms'):.0f} / {med(runs, 'p95_ms'):.0f} / {med(runs, 'p99_ms'):.0f} "
            f"| {med(runs, 'servscope_cpu_avg'):.0f}% "
            f"| {med(runs, 'servscope_rss_max_mb'):.0f} MB "
            f"| {med(runs, 'postgres_cpu_avg'):.0f}% |"
        )
    return "\n".join(lines)


def latency_table(data: dict[str, Any], what: str) -> str:
    lines = [
        f"| {what} | Requests/s | Failures | p50 / p95 / p99 (ms) | ServScope CPU | PostgreSQL CPU |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for level in data["levels"]:
        runs = [r for r in level["runs"] if r["valid"]]
        failures = sum(r["failures"] for r in runs)
        lines.append(
            f"| {level['users']} "
            f"| {med(runs, 'rps'):.1f} "
            f"| {failures} of {sum(r['requests'] for r in runs):,} "
            f"| {med(runs, 'p50_ms'):.0f} / {med(runs, 'p95_ms'):.0f} / {med(runs, 'p99_ms'):.0f} "
            f"| {med(runs, 'servscope_cpu_avg'):.0f}% "
            f"| {med(runs, 'postgres_cpu_avg'):.0f}% |"
        )
    return "\n".join(lines)


def endpoint_table(data: dict[str, Any], users: int) -> str:
    level = next(lv for lv in data["levels"] if lv["users"] == users)
    names = sorted(level["runs"][0]["endpoints"])
    lines = [
        "| Request | p50 (ms) | p95 (ms) | p99 (ms) | Count |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in names:
        eps = [r["endpoints"][name] for r in level["runs"] if name in r["endpoints"]]
        lines.append(
            f"| {name} | {med(eps, 'p50_ms'):.0f} | {med(eps, 'p95_ms'):.0f} "
            f"| {med(eps, 'p99_ms'):.0f} | {sum(e['requests'] for e in eps):,} |"
        )
    return "\n".join(lines)


def freshness_table(data: dict[str, Any]) -> str:
    runs = data["runs"]
    lines = [
        "| Run | Checkouts measured | p50 (ms) | p95 (ms) | p99 (ms) | min–max (ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(runs, 1):
        lines.append(
            f"| {i} | {r['measured']} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['p99_ms']:.0f} "
            f"| {r['min_ms']}–{r['max_ms']} |"
        )
    return "\n".join(lines)


def main() -> None:
    for path in sys.argv[1:]:
        data = json.loads(Path(path).read_text())
        print(f"\n### {path}\n")
        if data["workload"] == "ingest":
            print(ingest_table(data))
        elif data["workload"] == "freshness":
            print(freshness_table(data))
        else:
            what = "Users" if data["workload"] == "read" else "Checkouts/s"
            print(latency_table(data, what))
            if data["workload"] == "read":
                for level in data["levels"]:
                    print(f"\nPer request, {level['users']} users:\n")
                    print(endpoint_table(data, level["users"]))


if __name__ == "__main__":
    main()
