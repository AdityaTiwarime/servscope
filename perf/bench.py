"""Runs a Locust workload repeatedly and records what happened (Milestone 7).

For each load level (number of Locust users) it runs the workload `--runs` times, after one
warm-up run that is thrown away. During every run it samples, from the operating system,
ServScope's CPU and memory (the process listening on --servscope-port) and PostgreSQL's
CPU. It writes everything to a JSON file and prints a table.

    python perf/bench.py ingest --users 1,4,16 --runs 3 --seconds 45 --host http://localhost:8000
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import psutil

WORKLOADS = {
    "ingest": "perf/locust_ingest.py",
    "checkout": "perf/locust_checkout.py",
    "read": "perf/locust_read.py",
}
SPANS_PER_BATCH = 180


def listener(port: int) -> psutil.Process | None:
    for c in psutil.net_connections(kind="tcp"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
            return psutil.Process(c.pid)
    return None


class Sampler(threading.Thread):
    """Once a second: ServScope's CPU % (100 = one full core) and RSS, PostgreSQL's CPU %."""

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port, self.stop, self.rows = port, threading.Event(), []

    def run(self) -> None:
        app = listener(self.port)
        pg = [p for p in psutil.process_iter(["name"]) if p.info["name"] == "postgres"]
        for p in [app, *pg]:
            if p:
                p.cpu_percent(None)  # prime the counters
        while not self.stop.wait(1.0):
            row: dict[str, float] = {"t": time.time()}
            try:
                if app:
                    row["app_cpu"] = app.cpu_percent(None)
                    row["app_rss_mb"] = app.memory_info().rss / 2**20
            except psutil.Error:
                pass
            pg_cpu = 0.0
            for p in pg:
                try:
                    pg_cpu += p.cpu_percent(None)
                except psutil.Error:
                    pass
            row["pg_cpu"] = pg_cpu
            self.rows.append(row)


nan = float("nan")


def avg(samples: list[dict[str, float]], key: str) -> float:
    values = [s[key] for s in samples if key in s]
    return round(statistics.fmean(values), 1) if values else nan


def run_locust(args: argparse.Namespace, users: int, seconds: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "out"
        cmd = [
            sys.executable,
            "-m",
            "locust",
            "-f",
            WORKLOADS[args.workload],
            "--headless",
            "-u",
            str(users),
            "-r",
            str(users),
            "-t",
            f"{seconds}s",
            "--host",
            args.host,
            "--csv",
            str(prefix),
            "--only-summary",
            "--stop-timeout",
            "10",
            "--loglevel",
            "WARNING",
        ]
        if args.pin_locust is not None:
            cmd = ["taskset", "-c", args.pin_locust, *cmd]
        marks = Path(tmp) / "window"
        sampler = Sampler(args.servscope_port)
        sampler.start()
        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env={**os.environ, **args.env, "PERF_WINDOW_FILE": str(marks)},
        )
        sampler.stop.set()
        wall = time.monotonic() - started
        with open(f"{prefix}_stats.csv") as f:
            rows = list(csv.DictReader(f))
        window = (
            dict(line.split() for line in marks.read_text().splitlines()) if marks.exists() else {}
        )
    out = proc.stdout + proc.stderr
    by_name = {r["Name"]: r for r in rows}
    agg = by_name["Aggregated"]

    def num(v: str) -> float:
        return float("nan") if v in ("N/A", "") else float(v)

    def stats(r: dict[str, str]) -> dict[str, float]:
        return {
            "requests": int(r["Request Count"]),
            "failures": int(r["Failure Count"]),
            "rps": float(r["Requests/s"]),
            "fail_rps": float(r["Failures/s"]),
            "p50_ms": num(r["50%"]),
            "p95_ms": num(r["95%"]),
            "p99_ms": num(r["99%"]),
            "max_ms": float(r["Max Response Time"]),
        }

    # Only samples taken while load was running, minus the first second (users starting up).
    # Without the window marks the run is still reported, but its resource numbers are not.
    if "start" in window and "stop" in window:
        lo, hi = float(window["start"]) + 1, float(window["stop"])
        samples = [s for s in sampler.rows if lo <= s["t"] <= hi]
    else:
        samples = []
    result: dict[str, Any] = {
        "users": users,
        "wall_s": round(wall, 1),
        "valid": "INVALID RUN" not in out and int(agg["Request Count"]) > 0,
        **stats(agg),
        "endpoints": {n: stats(r) for n, r in by_name.items() if n != "Aggregated"},
        "resource_samples": len(samples),
        "servscope_cpu_avg": avg(samples, "app_cpu"),
        "servscope_rss_max_mb": round(
            max((s.get("app_rss_mb", 0) for s in samples), default=nan), 1
        ),
        "postgres_cpu_avg": avg(samples, "pg_cpu"),
    }
    if args.workload == "ingest":
        result["accepted_spans_per_s"] = round(
            (result["rps"] - result["fail_rps"]) * SPANS_PER_BATCH
        )
        result["refused_share"] = round(result["failures"] / max(result["requests"], 1), 3)
    return result


def median_of(runs: list[dict[str, Any]], key: str) -> float:
    return round(statistics.median(r[key] for r in runs), 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workload", choices=sorted(WORKLOADS))
    ap.add_argument("--users", default="8", help="comma-separated load levels")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--warmup", type=int, default=10, help="seconds of thrown-away warm-up")
    ap.add_argument("--host", required=True)
    ap.add_argument("--servscope-port", type=int, default=8000)
    ap.add_argument("--pin-locust", help="CPU list for Locust (taskset), e.g. 1")
    ap.add_argument("--reset", help="shell command run before every run, e.g. a TRUNCATE")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for the workload")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    args.env = dict(e.split("=", 1) for e in args.env)

    def reset() -> None:
        if args.reset:
            subprocess.run(shlex.split(args.reset), check=True, capture_output=True)

    levels = [int(u) for u in args.users.split(",")]
    reset()
    run_locust(args, levels[0], args.warmup)  # warm-up: caches, connection pools, JIT-free but cold
    report: dict[str, Any] = {
        "workload": args.workload,
        "label": args.label,
        "seconds": args.seconds,
        "runs_per_level": args.runs,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": {
            "cpus": psutil.cpu_count(),
            "memory_gb": round(psutil.virtual_memory().total / 2**30, 1),
        },
        "levels": [],
    }
    for users in levels:
        runs = []
        for i in range(args.runs):
            reset()
            r = run_locust(args, users, args.seconds)
            runs.append(r)
            extra = (
                f"  accepted {r['accepted_spans_per_s']:>7,} spans/s"
                f"  refused {r['refused_share']:.0%}"
                if args.workload == "ingest"
                else ""
            )
            print(
                f"users {users:>3} run {i + 1}: {r['rps']:8.1f} req/s"
                f"  p50 {r['p50_ms']:7.1f}  p95 {r['p95_ms']:7.1f}  p99 {r['p99_ms']:7.1f} ms"
                f"  fail {r['failures']:>5}{extra}"
                f"  ServScope CPU {r['servscope_cpu_avg']:5.1f}%"
                f"  RSS {r['servscope_rss_max_mb']:6.1f} MB"
                f"  PG CPU {r['postgres_cpu_avg']:5.1f}%{'' if r['valid'] else '  INVALID'}",
                flush=True,
            )
        report["levels"].append({"users": users, "runs": runs})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
