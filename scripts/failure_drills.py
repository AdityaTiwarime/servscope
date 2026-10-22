"""Failure drills: break the running stack on purpose and check what happens (Milestone 6).

Each drill causes a real failure (a stopped process, a killed process, a flood), measures
what the demo shop and ServScope did, and checks it against the behaviour the design
promises. Afterwards one invariant must hold over everything stored:

    metrics == spans   (per service, operation and minute: request and error counts)

A span lost on the way would make the metrics disagree with what the shop did; a retried
batch counted twice would make them larger. So the check is exact, not "roughly right".

It controls the stack only through the commands it is given, so the same drills run on a
local stack and under Docker Compose (see `--compose`). Exits non-zero if any check fails.

    python scripts/failure_drills.py --compose            # against `docker compose up`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

# Rotate products so a drill doesn't sell out one of them faster than inventory restocks.
CARTS = [{"items": [{"sku": sku, "qty": 1}]} for sku in ("tea", "mug", "beans")]
# A checkout's normal answers, and how many spans its trace has: 201 paid (9 spans), or 409
# out of stock (6: the order stops at inventory). Anything else would be a real failure.
NORMAL = {201: 9, 409: 6}
ARRIVAL_LIMIT_S = 150  # the Collector backs off up to 30 s between retries
FLOOD_SERVICE = "drill-flood"

# Per (service, operation, minute ended): what the stored spans say vs what the metrics say.
# Mirrors rollups.aggregate: SERVER spans only, the minute the span ended, and spans that
# arrived over 15 minutes late are stored but deliberately not counted.
INVARIANT_SQL = """
WITH s AS (
  SELECT service, operation,
         date_trunc('minute', to_timestamp((start_ns + duration_ns) / 1000000000)) AS m,
         count(*) AS n, count(*) FILTER (WHERE status_code = 2) AS e
  FROM spans
  WHERE kind = 2
    AND received_at - to_timestamp((start_ns + duration_ns) / 1000000000) <= interval '15 minutes'
  GROUP BY 1, 2, 3
), r AS (
  SELECT service, operation, minute AS m, request_count AS n, error_count AS e,
         (SELECT sum(x) FROM unnest(latency_buckets) x) AS b
  FROM metric_rollups
)
SELECT count(*) FROM s FULL JOIN r USING (service, operation, m)
WHERE coalesce(s.m, r.m) > now() - interval '3 hours'
  AND (coalesce(s.n, 0) <> coalesce(r.n, 0) OR coalesce(s.e, 0) <> coalesce(r.e, 0)
       OR coalesce(r.b, r.n, 0) <> coalesce(r.n, 0));
"""

COMPOSE = {
    "stop_servscope": "docker compose stop servscope",
    "start_servscope": "docker compose start servscope",
    "kill_servscope": "docker compose kill -s SIGKILL servscope",
    "stop_db": "docker compose stop postgres",
    "start_db": "docker compose start postgres",
    "stop_collector": "docker compose stop collector",
    "start_collector": "docker compose start collector",
    "stop_inventory": "docker compose stop inventory",
    "start_inventory": "docker compose start inventory",
    "sql": "docker compose exec -T postgres psql -U servscope -d servscope -At",
}


@dataclass
class Result:
    name: str
    checks: dict[str, bool] = field(default_factory=dict)
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


@dataclass
class Checkout:
    trace_id: str
    status: int
    ms: float


class Drills:
    def __init__(self, args: argparse.Namespace):
        self.gateway = httpx.AsyncClient(base_url=args.gateway, timeout=15)
        self.api = httpx.AsyncClient(base_url=args.api, timeout=10)
        self.cmd = {k: getattr(args, k) for k in COMPOSE}
        self.ingest_token = args.ingest_token

    # --- controlling the stack ---------------------------------------------------------

    def run(self, key: str) -> None:
        print(f"    $ {self.cmd[key]}", flush=True)
        subprocess.run(shlex.split(self.cmd[key]), check=True, capture_output=True)

    def sql(self, query: str) -> str:
        r = subprocess.run(
            shlex.split(self.cmd["sql"]), input=query, text=True, check=True, capture_output=True
        )
        return r.stdout.strip()

    async def get(self, path: str) -> httpx.Response | None:
        try:
            return await self.api.get(path)
        except httpx.HTTPError:
            return None

    async def wait_until(
        self, what: str, limit_s: float, check: Callable[[], Awaitable[bool]]
    ) -> float:
        start = time.monotonic()
        while time.monotonic() - start < limit_s:
            if await check():
                return time.monotonic() - start
            await asyncio.sleep(1)
        raise TimeoutError(f"{what}: not within {limit_s:.0f} s")

    async def ready(self) -> bool:
        r = await self.get("/readyz")
        return r is not None and r.status_code == 200

    async def shop_ready(self) -> bool:
        try:
            return (await self.gateway.get("/healthz")).status_code == 200
        except httpx.HTTPError:
            return False

    # --- traffic -----------------------------------------------------------------------

    async def checkouts(self, n: int, concurrency: int = 1) -> list[Checkout]:
        """n checkouts, each with a trace ID we choose, so we can look for it later."""
        sem = asyncio.Semaphore(concurrency)

        async def one(i: int) -> Checkout:
            async with sem:
                trace_id, parent = os.urandom(16).hex(), os.urandom(8).hex()
                start = time.perf_counter()
                try:
                    r = await self.gateway.post(
                        "/checkout",
                        json=CARTS[i % len(CARTS)],
                        headers={"traceparent": f"00-{trace_id}-{parent}-01"},
                    )
                    status = r.status_code
                except httpx.HTTPError:
                    status = 0
                return Checkout(trace_id, status, (time.perf_counter() - start) * 1000)

        return list(await asyncio.gather(*(one(i) for i in range(n))))

    async def complete(self, trace_id: str, spans: int) -> bool:
        r = await self.get(f"/api/traces/{trace_id}")
        return r is not None and r.status_code == 200 and r.json()["span_count"] >= spans

    async def arrived(self, done: list[Checkout]) -> tuple[int, float | None]:
        """Wait for every normally answered checkout's trace to be stored complete."""
        wanted = {c.trace_id: NORMAL[c.status] for c in done if c.status in NORMAL}
        pending = dict(wanted)
        start = time.monotonic()
        while pending and time.monotonic() - start < ARRIVAL_LIMIT_S:
            for trace_id, spans in list(pending.items()):
                if await self.complete(trace_id, spans):
                    del pending[trace_id]
            if pending:
                await asyncio.sleep(2)
        took = time.monotonic() - start if not pending else None
        return len(wanted) - len(pending), took

    def invariant(self) -> int:
        return int(self.sql(INVARIANT_SQL))

    async def ingest_refused(self) -> int:
        r = await self.get("/api/self")
        return (
            int(r.json()["ingest"]["ingest_refused"])
            if r is not None and r.status_code == 200
            else -1
        )


def normal(done: list[Checkout]) -> bool:
    return all(c.status in NORMAL for c in done)


def answers(done: list[Checkout]) -> dict[int, int]:
    return dict(sorted(Counter(c.status for c in done).items()))


def p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def latency(checkouts: list[Checkout]) -> dict[str, float]:
    ms = [c.ms for c in checkouts]
    return {
        "p50_ms": round(statistics.median(ms), 1),
        "p95_ms": p(ms, 0.95),
        "max_ms": round(max(ms), 1),
    }


def unchanged(before: dict[str, float], after: dict[str, float]) -> bool:
    """Latency 'unchanged': p95 within 1.5x the baseline plus 25 ms of scheduling noise."""
    return after["p95_ms"] <= before["p95_ms"] * 1.5 + 25


# --- the drills ----------------------------------------------------------------------


async def d6_inventory_stopped(d: Drills) -> Result:
    r = Result("D6 inventory stopped")
    d.run("stop_inventory")
    try:
        down = await d.checkouts(10)
    finally:
        d.run("start_inventory")
    r.measured["checkouts"] = [c.status for c in down]
    r.measured["slowest_ms"] = round(max(c.ms for c in down), 1)
    r.checks["every checkout fails with 503, not a hang"] = all(c.status == 503 for c in down)
    r.checks["it fails fast (under 1.5 s)"] = max(c.ms for c in down) < 1500
    trace = None
    if (
        await d.wait_until("a failed trace", 60, lambda: d.complete(down[0].trace_id, 4))
        is not None
    ):
        trace = (await d.api.get(f"/api/traces/{down[0].trace_id}")).json()
    causes = {s["error_type"] for s in trace["spans"]} if trace else set()
    r.measured["error types in the trace"] = sorted(c for c in causes if c)
    r.checks["the trace names the cause"] = "inventory_unavailable" in causes
    await d.wait_until("shop recovered", 60, d.shop_ready)
    after = await d.checkouts(5)
    r.checks["checkouts succeed again after inventory is back"] = all(
        c.status == 201 for c in after
    )
    return r


async def d4_collector_stopped(d: Drills) -> Result:
    r = Result("D4 collector stopped")
    base = await d.checkouts(30)
    d.run("stop_collector")
    try:
        down = await d.checkouts(30)
    finally:
        d.run("start_collector")
    r.measured["with collector"] = latency(base)
    r.measured["without collector"] = latency(down)
    r.measured["answers without collector"] = answers(down)
    r.checks["every checkout gets its normal answer"] = normal(base + down)
    r.checks["checkout latency unchanged"] = unchanged(latency(base), latency(down))
    await asyncio.sleep(20)  # give anything retried a chance to arrive
    got, _ = await d.arrived(down)
    r.measured["traces from the outage that arrived"] = f"{got} of {len(down)}"
    # Recorded, not asserted: the services' exporters retry for ~5 s, then drop (by design,
    # so the shop never blocks on telemetry). How many survive depends on timing.
    return r


async def d1_servscope_stopped(d: Drills) -> Result:
    r = Result("D1 ServScope stopped (N1)")
    base = await d.checkouts(40)
    d.run("stop_servscope")
    try:
        down = await d.checkouts(40)
    finally:
        d.run("start_servscope")
    r.measured["with ServScope"] = latency(base)
    r.measured["without ServScope"] = latency(down)
    r.measured["answers without ServScope"] = answers(down)
    r.checks["every checkout gets its normal answer while ServScope is down"] = normal(down)
    r.checks["checkout latency unchanged (N1)"] = unchanged(latency(base), latency(down))
    r.measured["ready again after (s)"] = round(
        await d.wait_until("ServScope ready", 90, d.ready), 1
    )
    got, took = await d.arrived(down)
    r.measured["traces from the outage stored complete"] = f"{got} of {len(down)}"
    r.measured["all arrived within (s)"] = None if took is None else round(took, 1)
    r.checks["the Collector delivered every trace from the outage"] = got == len(down) > 0
    return r


async def d2_database_stopped(d: Drills) -> Result:
    r = Result("D2 database stopped")
    refused_before = await d.ingest_refused()
    d.run("stop_db")
    try:
        await asyncio.sleep(3)
        down = await d.checkouts(20)
        services = await d.get("/api/services")
        readyz = await d.get("/readyz")
        self_r = await d.get("/api/self")
        await asyncio.sleep(8)  # let the Collector try (and be refused) at least once
        refused_during = await d.ingest_refused()
    finally:
        d.run("start_db")
    r.measured["answers without the database"] = answers(down)
    r.checks["every checkout gets its normal answer while the database is down"] = normal(down)
    r.checks["read API answers 503 problem+json"] = (
        services is not None
        and services.status_code == 503
        and services.headers.get("content-type") == "application/problem+json"
        and services.json().get("title") == "Database unavailable"
    )
    r.checks["/readyz says not ready"] = readyz is not None and readyz.status_code == 503
    r.checks["/api/self still answers, database unreachable"] = (
        self_r is not None
        and self_r.status_code == 200
        and not self_r.json()["database"]["reachable"]
    )
    r.measured["ingest batches refused during the outage"] = refused_during - refused_before
    r.checks["refused batches are counted"] = refused_during > refused_before
    r.measured["ready again after (s)"] = round(
        await d.wait_until("database back", 120, d.ready), 1
    )
    got, took = await d.arrived(down)
    r.measured["traces from the outage stored complete"] = f"{got} of {len(down)}"
    r.measured["all arrived within (s)"] = None if took is None else round(took, 1)
    r.checks["every trace from the outage arrived afterwards"] = got == len(down) > 0
    return r


async def d3_servscope_killed(d: Drills) -> Result:
    r = Result("D3 ServScope killed mid-traffic")
    traffic = asyncio.create_task(d.checkouts(40, concurrency=2))
    await asyncio.sleep(3)
    d.run("kill_servscope")  # SIGKILL: no clean shutdown, batches cut off mid-write
    await asyncio.sleep(3)
    d.run("start_servscope")
    done = await traffic
    r.measured["answers"] = answers(done)
    r.checks["every checkout gets its normal answer"] = normal(done)
    await d.wait_until("ServScope ready", 90, d.ready)
    got, took = await d.arrived(done)
    r.measured["traces stored complete"] = f"{got} of {len(done)}"
    r.measured["all arrived within (s)"] = None if took is None else round(took, 1)
    r.checks["no trace lost to the crash"] = got == len(done) > 0
    return r


def flood_batch(spans: int) -> bytes:
    """One OTLP batch of INTERNAL spans (not requests, so they don't touch metrics/alerts)."""
    trace_id, now = os.urandom(16), time.time_ns()
    batch = [
        Span(trace_id=trace_id, span_id=os.urandom(8), name="flood", kind=Span.SPAN_KIND_INTERNAL,
             start_time_unix_nano=now, end_time_unix_nano=now + 1_000_000)
        for _ in range(spans)
    ]  # fmt: skip
    resource = Resource(
        attributes=[KeyValue(key="service.name", value=AnyValue(string_value=FLOOD_SERVICE))]
    )
    request = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(resource=resource, scope_spans=[ScopeSpans(spans=batch)])]
    )
    return request.SerializeToString()


async def d5_ingest_flood(d: Drills, batches: int, spans: int, concurrency: int) -> Result:
    r = Result("D5 ingest flood (N2)")
    refused_before = await d.ingest_refused()
    sem = asyncio.Semaphore(concurrency)
    headers = {
        "Authorization": f"Bearer {d.ingest_token}",
        "Content-Type": "application/x-protobuf",
    }
    bodies = [flood_batch(spans) for _ in range(batches)]
    probes: list[float] = []

    async def send(body: bytes) -> httpx.Response:
        async with sem:
            return await d.api.post("/v1/traces", content=body, headers=headers, timeout=60)

    async def probe() -> None:  # is ServScope still answering while it is flooded?
        while True:
            start = time.perf_counter()
            await d.api.get("/healthz")
            probes.append((time.perf_counter() - start) * 1000)
            await asyncio.sleep(0.5)

    prober = asyncio.create_task(probe())
    start = time.monotonic()
    replies = await asyncio.gather(*(send(b) for b in bodies))
    took = time.monotonic() - start
    prober.cancel()
    codes = [x.status_code for x in replies]
    ok, busy = codes.count(200), codes.count(503)
    r.measured["batches"] = f"{batches} x {spans} spans, {concurrency} at once, in {took:.1f} s"
    r.measured["accepted (200)"] = ok
    r.measured["refused (503)"] = busy
    r.measured["slowest /healthz during the flood (ms)"] = round(max(probes), 1) if probes else None
    r.checks["only 200 or 503, nothing else"] = ok + busy == batches
    r.checks["overload was actually reached (some 503s)"] = busy > 0
    r.checks["every 503 says when to retry"] = all(
        x.headers.get("Retry-After") for x in replies if x.status_code == 503
    )
    r.checks["ServScope kept answering during the flood"] = bool(probes) and max(probes) < 2000
    stored = int(d.sql(f"SELECT count(*) FROM spans WHERE service = '{FLOOD_SERVICE}'"))  # noqa: S608
    r.measured["flood spans stored"] = stored
    r.checks["acknowledged means stored, exactly"] = stored == ok * spans
    refused_after = await d.ingest_refused()
    r.checks["every refusal is counted"] = refused_after - refused_before >= busy
    d.sql(f"DELETE FROM spans WHERE service = '{FLOOD_SERVICE}'")  # noqa: S608  # tidy up
    return r


async def main(args: argparse.Namespace) -> list[Result]:
    d = Drills(args)
    await d.wait_until("ServScope ready", 120, d.ready)
    await d.wait_until("shop ready", 120, d.shop_ready)
    chosen = args.only.split(",") if args.only else ["d6", "d4", "d1", "d2", "d3", "d5"]
    drills: dict[str, Callable[[], Awaitable[Result]]] = {
        "d6": lambda: d6_inventory_stopped(d),
        "d4": lambda: d4_collector_stopped(d),
        "d1": lambda: d1_servscope_stopped(d),
        "d2": lambda: d2_database_stopped(d),
        "d3": lambda: d3_servscope_killed(d),
        "d5": lambda: d5_ingest_flood(
            d, args.flood_batches, args.flood_spans, args.flood_concurrency
        ),
    }
    results: list[Result] = []
    for key in chosen:
        print(f"\n== {key} ==", flush=True)
        try:
            result = await drills[key]()
        except Exception as exc:  # a drill that crashes is a failed drill, not a crashed run
            result = Result(key, {f"drill ran to the end ({type(exc).__name__}: {exc})": False})
        results.append(result)
        for name, value in result.measured.items():
            print(f"    {name}: {value}")
        for name, ok in result.checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}", flush=True)

    await asyncio.sleep(5)
    final = Result("metrics agree with the stored spans")
    mismatches = d.invariant()
    final.measured["(service, operation, minute) rows that disagree"] = mismatches
    final.checks["metrics == spans, exactly, after every drill"] = mismatches == 0
    results.append(final)
    print(f"\n== invariant ==\n    rows that disagree: {mismatches}")
    print(f"  {'PASS' if mismatches == 0 else 'FAIL'}  metrics == spans, exactly")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--ingest-token", default=os.environ.get("SERVSCOPE_INGEST_TOKEN", ""))
    parser.add_argument("--compose", action="store_true", help="use docker compose commands")
    parser.add_argument("--only", help="comma-separated drills, e.g. d1,d5")
    parser.add_argument("--json", help="also write the results to this file")
    parser.add_argument("--flood-batches", type=int, default=150)
    parser.add_argument("--flood-spans", type=int, default=1000)
    parser.add_argument("--flood-concurrency", type=int, default=48)
    for key in COMPOSE:
        parser.add_argument(f"--{key.replace('_', '-')}", dest=key, default=None)
    parsed = parser.parse_args()
    for key, command in COMPOSE.items():
        if getattr(parsed, key) is None:
            if not parsed.compose:
                parser.error(f"--{key.replace('_', '-')} is required without --compose")
            setattr(parsed, key, command)
    if not parsed.ingest_token:
        parser.error(
            "set SERVSCOPE_INGEST_TOKEN or --ingest-token (the flood drill sends to ingest)"
        )
    results = asyncio.run(main(parsed))
    if parsed.json:
        summary = [{"name": x.name, "checks": x.checks, "measured": x.measured} for x in results]
        with open(parsed.json, "w") as f:
            json.dump(summary, f, indent=2)
    failed = [x.name for x in results if not x.passed]
    print("\nALL DRILLS PASSED" if not failed else f"\nFAILED: {', '.join(failed)}")
    sys.exit(1 if failed else 0)
