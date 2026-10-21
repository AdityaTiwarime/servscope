"""End-to-end smoke test: one checkout through the real pipeline.

    gateway -> orders -> inventory, payments   (each exports spans to the Collector)
    Collector --OTLP/HTTP--> ServScope --> PostgreSQL

It sends the checkout with its own W3C `traceparent`, so it knows the trace ID in advance,
then polls the database until that trace's server spans from all four services arrive, and
checks the shape of the stored trace.

Usage: python scripts/smoke.py --gateway http://localhost:8080 --db postgresql://...
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import asyncpg
import httpx

SERVICES = {"gateway", "orders", "inventory", "payments"}
SERVER, CLIENT = 2, 3  # OTLP SpanKind values


async def wait_for_collector(collector: str, wait_seconds: float) -> bool:
    """Spans sent before the Collector listens are retried by the services' exporters for a
    few seconds and then dropped (by design: the shop never waits for telemetry). So the
    smoke test only starts once the Collector accepts connections."""
    deadline = time.monotonic() + wait_seconds
    async with httpx.AsyncClient(timeout=2) as http:
        while time.monotonic() < deadline:
            try:
                await http.post(f"{collector}/v1/traces", content=b"")
                return True  # any HTTP answer means it is listening
            except httpx.TransportError:
                await asyncio.sleep(0.5)
    return False


async def main(gateway: str, collector: str, db: str, wait_seconds: float) -> int:
    if not await wait_for_collector(collector, wait_seconds):
        print(f"FAIL  collector at {collector} never became reachable")
        return 1
    trace_id = os.urandom(16).hex()
    caller_span = os.urandom(8).hex()
    headers = {"traceparent": f"00-{trace_id}-{caller_span}-01"}

    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(
            f"{gateway}/checkout",
            json={"items": [{"sku": "tea", "qty": 2}, {"sku": "mug", "qty": 1}]},
            headers=headers,
        )
    print(f"checkout -> {r.status_code} {r.text}")
    if r.status_code != 201:
        return 1

    conn = await asyncpg.connect(db)
    deadline = time.monotonic() + wait_seconds
    rows: list[asyncpg.Record] = []
    while time.monotonic() < deadline:
        rows = await conn.fetch(
            "SELECT span_id, parent_span_id, service, operation, kind, status_code "
            "FROM spans WHERE trace_id = $1 ORDER BY start_ns",
            trace_id,
        )
        # Each service exports on its own schedule, so wait for all four server spans.
        if {row["service"] for row in rows if row["kind"] == SERVER} >= SERVICES:
            break
        await asyncio.sleep(0.5)
    await conn.close()

    for row in rows:
        print(
            f"  {row['span_id']} parent={row['parent_span_id']} "
            f"{row['service']:<9} kind={row['kind']} {row['operation']}"
        )
    spans = {row["span_id"]: row for row in rows}
    servers = {row["service"]: row for row in rows if row["kind"] == SERVER}

    def called_from(service: str, caller: str) -> bool:
        """service's server span <- a CLIENT span of caller <- caller's server span."""
        if service not in servers or caller not in servers:
            return False
        call = spans.get(servers[service]["parent_span_id"])
        return (
            call is not None
            and call["kind"] == CLIENT
            and call["service"] == caller
            and call["parent_span_id"] == servers[caller]["span_id"]
        )

    roots = [row for row in rows if row["parent_span_id"] not in spans]
    checks = {
        "all four services reported a server span": set(servers) >= SERVICES,
        "exactly one span continues the caller's trace": len(roots) == 1
        and roots[0]["parent_span_id"] == caller_span
        and roots[0]["service"] == "gateway",
        "orders was called by the gateway": called_from("orders", "gateway"),
        "inventory was called by orders": called_from("inventory", "orders"),
        "payments was called by orders": called_from("payments", "orders"),
        "no span is marked as an error": all(row["status_code"] != 2 for row in rows),
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="http://localhost:8080")
    p.add_argument("--collector", default="http://localhost:4318")
    p.add_argument("--db", default="postgresql://servscope:servscope@localhost:5432/servscope")
    p.add_argument("--wait-seconds", type=float, default=30)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.gateway, a.collector, a.db, a.wait_seconds)))
