"""W4 read API: people looking at the dashboard, against 24 h of seeded history.

Requests are exactly what the dashboard's screens send. Users mostly sit on the Overview,
which refreshes its queries; some open traces, search logs, or look at the last 24 hours.
"Now" is the end of the seeded history (PERF_NOW, ISO 8601), so every query looks at real
data however long after seeding the benchmark runs.

    PERF_NOW=2026-10-22T16:00:00Z locust -f perf/locust_read.py --headless -u 10 -t 60s ...
"""

from __future__ import annotations

import hashlib
import os
import random
from datetime import datetime, timedelta

import window  # noqa: F401  (marks the measured window for bench.py)
from locust import HttpUser, between, task

NOW = datetime.fromisoformat(os.environ["PERF_NOW"])
SEEDED_CHECKOUTS = int(os.environ.get("PERF_CHECKOUTS", "172800"))
SERVICES = ("gateway", "orders", "inventory", "payments")


def last(minutes: int) -> dict[str, str]:
    return {"from": (NOW - timedelta(minutes=minutes)).isoformat(), "to": NOW.isoformat()}


def seeded_trace_id(n: int) -> str:
    return hashlib.md5(f"seed{n}".encode()).hexdigest()  # matches seed.sql


class DashboardUser(HttpUser):
    wait_time = between(1, 3)

    @task(6)
    def overview(self) -> None:  # one refresh of the Overview screen, last 15 minutes
        w = last(15)
        self.client.get("/api/services", params=w, name="services 15m")
        self.client.get("/api/dependencies", params=w, name="dependencies 15m")
        self.client.get(
            "/api/traces", params={**w, "status": "error", "limit": 8}, name="failing traces 15m"
        )
        for s in SERVICES:
            self.client.get(
                f"/api/services/{s}/metrics", params={**w, "step": "1m"}, name="metrics 15m"
            )

    @task(3)
    def open_a_trace(self) -> None:  # click a trace: waterfall plus its logs
        tid = seeded_trace_id(random.randrange(SEEDED_CHECKOUTS))
        self.client.get(f"/api/traces/{tid}", name="trace detail")
        self.client.get("/api/logs", params={"trace_id": tid}, name="logs of a trace")

    @task(1)
    def search_logs(self) -> None:
        self.client.get("/api/logs", params={**last(60), "q": "declined"}, name="log search 1h")

    @task(1)
    def search_traces(self) -> None:
        params = {**last(60), "service": "payments", "status": "error"}
        self.client.get("/api/traces", params=params, name="trace search 1h")

    @task(1)
    def last_day(self) -> None:  # the 24h range on the Overview
        w = last(24 * 60)
        self.client.get("/api/services", params=w, name="services 24h")
        self.client.get(
            "/api/services/gateway/metrics", params={**w, "step": "5m"}, name="metrics 24h"
        )
