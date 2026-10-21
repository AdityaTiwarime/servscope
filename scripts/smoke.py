"""End-to-end smoke test: one checkout through the real pipeline.

    gateway --OTLP--> Collector --OTLP/HTTP--> ServScope --> PostgreSQL

It sends the checkout with its own W3C `traceparent`, so it knows the trace ID in advance,
then polls the database until that trace's spans arrive, and checks their shape.

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


async def main(gateway: str, db: str, wait_seconds: float) -> int:
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
            "SELECT span_id, parent_span_id, service, operation FROM spans WHERE trace_id = $1",
            trace_id,
        )
        # Wait for the spans we check, not just any two: a trace can arrive in several batches.
        if {"POST /checkout", "price_cart"} <= {row["operation"] for row in rows}:
            break
        await asyncio.sleep(0.5)
    await conn.close()

    for row in rows:
        print(
            f"  span {row['span_id']} parent={row['parent_span_id']} "
            f"{row['service']} {row['operation']}"
        )
    by_op = {row["operation"]: row for row in rows}
    server = by_op.get("POST /checkout")
    child = by_op.get("price_cart")
    checks = {
        "trace stored": bool(rows),
        "server span continues the caller's trace": server is not None
        and server["parent_span_id"] == caller_span,
        "price_cart is a child of the server span": child is not None
        and server is not None
        and child["parent_span_id"] == server["span_id"],
        "service is gateway": bool(rows) and {row["service"] for row in rows} == {"gateway"},
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="http://localhost:8080")
    p.add_argument("--db", default="postgresql://servscope:servscope@localhost:5432/servscope")
    p.add_argument("--wait-seconds", type=float, default=30)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.gateway, a.db, a.wait_seconds)))
