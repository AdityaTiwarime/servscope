"""W3 freshness: how long after a checkout until ServScope can show its whole trace.

This is the end-to-end telemetry latency: the services' export batching, the Collector's
batching, ingest and the database. It is measured separately from the demo app's own
latency (W1), as the project brief asks. One checkout at a time, each followed by polling
the API every 50 ms until all nine spans are there.

    python perf/freshness.py --gateway http://localhost:8080 --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import httpx


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 0)


def one_run(gw: httpx.Client, api: httpx.Client, n: int) -> list[float]:
    delays: list[float] = []
    for _ in range(n):
        trace_id = os.urandom(16).hex()
        tp = {"traceparent": f"00-{trace_id}-{os.urandom(8).hex()}-01"}
        r = gw.post("/checkout", json={"items": [{"sku": "tea", "qty": 1}]}, headers=tp)
        answered = time.monotonic()
        if r.status_code != 201:
            continue
        while time.monotonic() - answered < 30:
            t = api.get(f"/api/traces/{trace_id}")
            if t.status_code == 200 and t.json()["span_count"] >= 9:
                delays.append((time.monotonic() - answered) * 1000)
                break
            time.sleep(0.05)
        time.sleep(0.5)
    return delays


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--checkouts", type=int, default=60)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    runs = []
    with (
        httpx.Client(base_url=args.gateway, timeout=10) as gw,
        httpx.Client(base_url=args.api, timeout=10) as api,
    ):
        for i in range(args.runs):
            d = one_run(gw, api, args.checkouts)
            res = {
                "measured": len(d),
                "p50_ms": pct(d, 0.5),
                "p95_ms": pct(d, 0.95),
                "p99_ms": pct(d, 0.99),
                "min_ms": round(min(d)),
                "max_ms": round(max(d)),
                "mean_ms": round(statistics.fmean(d)),
            }
            runs.append(res)
            print(f"run {i + 1}: {res}", flush=True)
    Path(args.out).write_text(json.dumps({"workload": "freshness", "runs": runs}, indent=2))


if __name__ == "__main__":
    main()
