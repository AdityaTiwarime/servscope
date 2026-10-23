# Performance (Milestone 7)

What was measured, how, on what machine, and what changed. Every number below was produced
by `perf/bench.py` or `perf/freshness.py` and copied into this file by `perf/report.py`. The raw
results, every run of them, are in `perf/results/*.json`. Nothing here is estimated.

## Summary

- **Ingest:** one profiled change (ORM bulk insert → SQLAlchemy Core insert) raised sustained
  ingest from about **3,400 to about 17,000 spans/s** on one CPU core. Median export latency at
  4 concurrent exports fell from **210 ms to 41 ms**.
- **Ingest overload:** at 32 concurrent exporters, the old code refused 22% of batches with
  503 + Retry-After, as designed. The new code refused none. Memory stayed flat (86–95 MB) at
  every level, before and after.
- **End-to-end telemetry latency:** from a checkout's answer to its whole trace being readable
  in ServScope takes **about 1.48 s (p50) and 1.53 s (p95)**. Almost all of that is deliberate
  batching (1 s in the services, up to 2 s in the Collector), not ServScope.
- **Read API:** against 24 h of history (1.56 million spans), the dashboard's requests have
  **p50 7 ms, p95 44 ms and p99 210 ms** at 30 users (about 64 requests/s), with no failures.
- **Demo shop:** checkout latency is reported apart from telemetry latency, as the brief asks.
  It is the shop's own speed, and ServScope is not on that path.

## Machine and versions

| | |
| --- | --- |
| CPU | 2 vCPU, Intel Xeon @ 2.10 GHz (a cloud VM) |
| Memory | 7 GB |
| OS | Linux 6.18.44 |
| Python | 3.12.3 |
| ServScope stack | FastAPI 0.157.0, uvicorn 0.54.0, SQLAlchemy 2.1.5, asyncpg 0.32.0, protobuf 7.36.2 |
| Database | PostgreSQL 16.15, shared_buffers 128 MB (defaults otherwise) |
| Collector | OpenTelemetry Collector contrib 0.149.0 (`deploy/collector.yaml`) |
| Load generator | Locust 2.47.0 |

**Pinning.** ServScope (one uvicorn process) runs on CPU 0 (`taskset -c 0`). Locust runs on CPU 1
(`--pin-locust 1`), so the load generator does not steal ServScope's core. PostgreSQL, the
Collector and the demo services are not pinned. Everything shares one VM, so these are
numbers *for this machine*: their purpose is comparing before with after, not sizing a
production deployment.

## Method

- Every load level is run **3 times**. Tables show the median, with the lowest–highest range
  of the 3 runs where it matters. A warm-up run before each sweep is thrown away.
- Latency percentiles (p50/p95/p99) come from Locust's own statistics. Locust rounds values
  above 100 ms to two significant figures (so 1,234 ms shows as 1,200).
- Resource use is sampled once a second from the operating system (psutil): ServScope's CPU
  (100% = one full core) and peak memory (RSS), and the sum over all PostgreSQL processes.
  Only samples taken while load was running count. Each workload marks its start and stop
  (`perf/window.py`).
- Before and after use **the same workload, settings and machine**. The "before" code was
  run from a clean checkout of the previous commit, back to back with the "after" code.
- Ingest tables are emptied before every run, so each run starts from the same database.

Reproduce, with the stack running and ServScope pinned:

```bash
uv run python perf/bench.py ingest --users 1,2,4,8,16,32 --runs 3 --seconds 45 --warmup 10 \
  --host http://127.0.0.1:8000 --pin-locust 1 --env PERF_BODIES=6000 \
  --reset "psql -U servscope -d servscope -qc 'TRUNCATE spans, logs, metric_rollups'" \
  --out perf/results/ingest.json
uv run python perf/report.py perf/results/ingest.json
```

## Workloads

| | What it does | File |
| --- | --- | --- |
| W1 checkout | Customers buying in the demo shop: each user 1 checkout/s, rotating carts. Measures the shop, not ServScope | `perf/locust_checkout.py` |
| W2 ingest | Exporters sending OTLP/HTTP protobuf, gzip, 180 spans per batch (20 nine-span checkout traces), as fast as answers come back (closed loop). Every span is new: bodies are built before the run with fresh IDs, and a run that runs out of bodies is marked invalid | `perf/locust_ingest.py` |
| W3 freshness | One checkout at a time with a known trace ID, then the read API polled every 50 ms until all 9 spans are there | `perf/freshness.py` |
| W4 read | Dashboard users against 24 h of seeded history (172,800 checkouts, 1,555,200 spans, 518,400 logs, `perf/seed.sql`). Mostly Overview refreshes, also opening traces, searching logs and traces, and the 24 h range | `perf/locust_read.py` |

## W2: ingest, and the optimisation

### Baseline

| Concurrent exports | Accepted spans/s | Refused | Export p50 / p95 / p99 (ms) | ServScope CPU | ServScope memory | PostgreSQL CPU |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2,373 (2,341–2,524) | 0% | 73 / 100 / 120 | 73% | 86 MB | 21% |
| 2 | 3,411 (3,249–3,501) | 0% | 99 / 150 / 180 | 96% | 87 MB | 24% |
| 4 | 3,329 (3,328–3,556) | 0% | 210 / 310 / 360 | 98% | 89 MB | 26% |
| 8 | 3,573 (3,534–3,581) | 0% | 370 / 610 / 720 | 98% | 91 MB | 27% |
| 16 | 3,434 (3,311–3,580) | 0% | 810 / 1100 / 1200 | 98% | 92 MB | 28% |
| 32 | 3,495 (3,445–3,554) | 22% | 1300 / 1600 / 1700 | 98% | 92 MB | 28% |

ServScope's core is full from 2 concurrent exports, while PostgreSQL uses about a quarter of
a core. Past that point, more exporters only queue: throughput stays flat and latency
doubles with each doubling of load. At 32 exporters, batches waiting longer than a second for
a write slot get 503 + Retry-After (the Collector retries them), as designed in Milestone 6.

### Profile

py-spy sampled ServScope for 25 s at 4 concurrent exports (2,261 samples):

| Where the CPU went | Share |
| --- | --- |
| SQLAlchemy ORM bulk-insert path (`Session.execute` with an ORM `insert()`) | 44% |
| asyncpg sending the rows | 16.5% |
| SQL compilation cache keys | 8% |
| Decoding protobuf | 5.8% |
| Scrubbing secrets from attributes | 2.1% |
| Building rollups | ~1% |

The rows are plain dictionaries that are only inserted and never loaded as objects. The ORM's
per-row bookkeeping was doing work nobody used.

### Optimisation 1: Core inserts

`ingest.py` and `rollups.py` now run the same `INSERT ... ON CONFLICT DO NOTHING RETURNING`
on a Core connection (`engine.begin()`) instead of an ORM session. The SQL, the transaction
(spans and their rollups commit together) and the duplicate handling are unchanged. All 220
tests pass unchanged.

Was it real? After a 4-user run, the database held exactly as many spans as ServScope
reported storing: 407,880, 9 per trace, 0 duplicates, 0 rejected. The rollups count 181,280
requests, the same as the number of server spans.

### After

| Concurrent exports | Accepted spans/s | Refused | Export p50 / p95 / p99 (ms) | ServScope CPU | ServScope memory | PostgreSQL CPU |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 10,957 (10,729–11,334) | 0% | 16 / 21 / 26 | 65% | 86 MB | 27% |
| 2 | 15,284 (14,845–15,469) | 0% | 22 / 33 / 42 | 81% | 88 MB | 35% |
| 4 | 16,795 (15,820–17,937) | 0% | 41 / 62 / 82 | 88% | 90 MB | 40% |
| 8 | 18,168 (17,002–18,283) | 0% | 76 / 110 / 130 | 94% | 93 MB | 41% |
| 16 | 17,391 (16,935–18,252) | 0% | 160 / 220 / 280 | 95% | 94 MB | 42% |
| 32 | 17,293 (16,723–17,877) | 0% | 320 / 410 / 480 | 94% | 95 MB | 43% |

| Same workload | Before | After |
| --- | --- | --- |
| Peak sustained ingest | ~3,500 spans/s | ~17,300–18,200 spans/s |
| Export p50 at 4 exporters | 210 ms | 41 ms |
| Export p99 at 32 exporters | 1,700 ms | 480 ms |
| Batches refused at 32 exporters | 22% | 0% |

The knee is still where ServScope's single core fills (now around 4–8 exporters). Beyond it,
latency again grows with load while throughput holds. The next limit is the same process
(the remaining SQL work and asyncpg), so more throughput would come from more uvicorn
workers on more cores. That is a deployment choice, not a code change, so it was not
measured here.

A second optimisation was allowed but not made: no other hotspot was large enough to justify one.

## W3: end-to-end telemetry latency

From the moment the shop answers a checkout until ServScope's API returns all 9 spans of
that trace (40 checkouts per run, one at a time):

| Run | Checkouts measured | p50 (ms) | p95 (ms) | p99 (ms) | min–max (ms) |
| --- | --- | --- | --- | --- | --- |
| 1 | 40 | 1485 | 1532 | 2480 | 1441–2480 |
| 2 | 40 | 1481 | 1529 | 1534 | 1461–1534 |
| 3 | 40 | 1483 | 1526 | 1543 | 1434–1543 |

The ~1.5 s is the batching configured on purpose: each service's span processor exports
every 1 s, and the Collector sends a batch after 2 s or 512 spans, whichever comes first.
Ingest and the database add a few milliseconds (see W2 at 1 exporter). The one 2.5 s outlier
is a trace that just missed one batch window and waited for the next. Shorter batching
would cut this at the cost of more, smaller requests.

## W1: the demo shop (not ServScope)

Checkout latency seen by customers, with ServScope receiving the resulting telemetry:

| Checkouts/s | Requests/s | Failures | p50 / p95 / p99 (ms) | ServScope CPU | PostgreSQL CPU |
| --- | --- | --- | --- | --- | --- |
| 10 | 10.2 | 0 of 1,345 | 55 / 73 / 86 | 1% | 0% |
| 30 | 30.1 | 0 of 4,027 | 160 / 220 / 250 | 2% | 2% |

ServScope uses 1–3% of a core for this traffic. The shop's latency grows with load because
its four services share the second CPU with Locust. It is the demo's own speed, kept apart
from telemetry latency (W3).

## W4: read API

| Users | Requests/s | Failures | p50 / p95 / p99 (ms) | ServScope CPU | PostgreSQL CPU |
| --- | --- | --- | --- | --- | --- |
| 10 | 20.4 | 0 of 2,701 | 6 / 46 / 240 | 5% | 10% |
| 30 | 63.7 | 0 of 8,335 | 7 / 44 / 210 | 15% | 32% |

Per request, 30 users:

| Request | p50 (ms) | p95 (ms) | p99 (ms) | Count |
| --- | --- | --- | --- | --- |
| dependencies 15m | 31 | 73 | 320 | 958 |
| failing traces 15m | 12 | 42 | 160 | 957 |
| log search 1h | 5 | 12 | 81 | 169 |
| logs of a trace | 4 | 120 | 210 | 497 |
| metrics 15m | 4 | 22 | 35 | 3,828 |
| metrics 24h | 42 | 100 | 350 | 149 |
| services 15m | 8 | 52 | 140 | 960 |
| services 24h | 32 | 76 | 140 | 150 |
| trace detail | 6 | 230 | 370 | 497 |
| trace search 1h | 24 | 75 | 220 | 170 |

Reads are not the bottleneck: at 30 users ServScope uses 15% of its core and PostgreSQL a
third of one. The slowest requests are the ones that read the most: the dependency map
(joins client and server spans) and the 24 h range. Opening a random trace from the 1.5
million is slow the first time (p50 97 ms in the 10-user runs, which ran first), most likely because its
rows are not yet in memory. By the 30-user runs more of the table was cached (p50 6 ms). The
tail (p95/p99) is that cold-read cost.

## Limits of these numbers

- One small VM runs everything. ServScope, PostgreSQL and the Collector compete for two cores,
  so absolute numbers would differ on other hardware. The before/after comparison is the
  point.
- One uvicorn worker. Multiple workers would scale ingest across cores. Not measured.
- PostgreSQL ran with near-default settings (128 MB shared buffers). A tuned database would
  change the read tail, not the ingest result (ServScope's CPU was the limit, not PostgreSQL's).
- Ingest batches are synthetic, but shaped like the demo's traces (9 spans, the demo's
  attributes, gzip protobuf like the Collector sends). Real traffic with large attributes
  would cost more decoding per span.
- Locust rounds percentiles above 100 ms to two significant figures.
