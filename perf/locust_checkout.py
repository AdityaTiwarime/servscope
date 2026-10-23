"""W1 checkout: customers buying from the demo shop, with the whole telemetry pipeline on.

Each user places one checkout per second (an open, fixed-rate load: constant_throughput),
so the number of users is the request rate. Products rotate so no single one sells out;
run inventory with RESTOCK_EVERY_S=1 so stock never limits the run. Anything but 201 is a
failure.

    locust -f perf/locust_checkout.py --headless -u 20 -t 60s --host http://localhost:8080
"""

from __future__ import annotations

import itertools

import window  # noqa: F401  (marks the measured window for bench.py)
from locust import FastHttpUser, constant_throughput, task

CARTS = itertools.cycle([{"items": [{"sku": sku, "qty": 1}]} for sku in ("tea", "mug", "beans")])


class Customer(FastHttpUser):
    wait_time = constant_throughput(1.0)

    @task
    def checkout(self) -> None:
        with self.client.post(
            "/checkout", json=next(CARTS), name="checkout", catch_response=True
        ) as r:
            if r.status_code != 201:
                r.failure(f"{r.status_code}")
