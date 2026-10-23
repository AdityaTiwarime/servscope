# Interview pack

Everything needed to present ServScope in an interview: the pitch, how to explain each part,
the questions likely to come up with answers grounded in this repository, and resume bullets.
Every number here is measured and linked to where it was measured. Don't round them up.

## The pitch

**30 seconds.** "ServScope is an observability platform I built for a four-service demo shop.
The services send OpenTelemetry traces and logs through a Collector; ServScope stores them in
PostgreSQL, builds per-minute metrics as data arrives, alerts on what customers feel, and
links each incident to the traces that show the cause. I tested it by breaking it on purpose
and by load testing it. One profiled change made ingest about five times faster."

**2 minutes.** Add, in this order:

1. *The architecture*: shop → Collector → ServScope (FastAPI) → PostgreSQL, a React dashboard
   served by ServScope ([architecture.md](architecture.md), diagram 1).
2. *One hard guarantee*: a batch is acknowledged only after it is committed, and retries
   can't create duplicates or double-count metrics ([ADR 0002](decisions/0002-ack-after-commit-idempotent-ingest.md)).
3. *One real bug it found*: a timed-out order was still charged; fixed with deadline
   propagation and a void ([ADR 0006](decisions/0006-deadline-and-void.md)).
4. *One measured result*: ~3,400 → ~17,000 spans/s after profiling showed 44% of CPU in the
   ORM ([performance.md](performance.md)).

## Explaining each area

### Architecture

- Why a Collector in between: services export in the background with bounded queues and never
  wait for telemetry. The Collector batches and retries, so ServScope can be restarted
  without the shop noticing (drill D4: checkout p95 12.1 ms with the Collector, 14.3 ms
  without).
- Why one PostgreSQL: one stateful system, and spans plus their metrics commit in one
  transaction. Limits on windows and a statement timeout keep queries bounded
  ([ADR 0001](decisions/0001-postgresql-for-everything.md)).
- Why metrics at ingest: alerts read metrics every 15 s; scanning raw spans would not scale.
  Only newly inserted spans count ([ADR 0003](decisions/0003-rollups-at-ingest.md)).

### The debugging workflow (what the product is for)

Start from symptoms customers feel (the gateway's error rate and p95), not from a guess.
An incident links up to 5 traces from the window the rule judged. The trace waterfall shows
the critical path and "where the time went". Log lines are linked to spans by trace and span
ID. In the payments-timeout demo: gateway errors → linked trace → orders' call to payments cut
at 2 s → the log line says the charge was voided. Walk through it with
[demo-script.md](demo-script.md).

### Reliability

- *Acknowledged means stored*, *idempotent writes*, *backpressure instead of queues*
  ([ADR 0004](decisions/0004-backpressure-with-503.md)).
- Proved with drills, not claimed: database stopped (ingest 503s, Collector retries, all 20
  traces from the outage stored within 4.3 s of it coming back); ServScope killed mid-write;
  a flood of 150 × 1,000 spans, 48 at once (exactly 113,000 spans stored for 113 accepted
  batches, every refusal a 503 with Retry-After). After every drill, an exact check that
  the per-minute metrics equal the stored spans ([reliability.md](reliability.md)).
- Alerts on settled data with `for`/`recover`, one incident per outage enforced by a unique
  index, state stored so restarts resume timers ([ADR 0005](decisions/0005-alerts-on-settled-data.md)).

### Security

- Ingest token and admin token compared in constant time. Admin token in the
  `Authorization` header only (no cookie, so no CSRF), kept in memory in the dashboard,
  lockout after 10 wrong tokens per minute per address.
- Secrets scrubbed before storage (password/token fields, `password=` pairs, card-number
  shapes). Tests check no card token ever reaches a span.
- Size limits on the body *and* the unzipped body (gzip bombs), attributes, page sizes and
  time windows. Errors are problem+json with a request ID and never leak internals.
- No CORS at all (the dashboard is same-origin) and a strict Content-Security-Policy.
- Failure scenarios behind three locks; ServScope refuses to start in production with
  injection on. Audit log written in the same transaction as each change.
- CI: gitleaks over the whole history, pip-audit and npm audit on every push.
- Honest gap: reading needs no login, so it must stay on localhost or a private network.

### Performance

- Method: Locust, ServScope pinned to one core and the load generator to the other, 3 runs
  per level, medians with ranges, same workload before and after, raw JSON committed.
- Ingest saturated one core at ~3,400 spans/s; py-spy showed 44% in the ORM bulk-insert path.
  Core inserts: ~17,000 spans/s, p50 at 4 exporters 210 → 41 ms, 0% refused at 32 exporters
  (was 22%). Verified by counting rows in the database ([ADR 0007](decisions/0007-core-inserts.md)).
- End-to-end telemetry latency ~1.5 s, nearly all deliberate batching. Read API p99 210 ms at
  30 users over 1.5 M spans. The demo's own latency is reported separately.

## Likely questions, with answers

**Why not use Jaeger / Grafana / Datadog?**
For a product, you would. The point here was to build and measure the hard parts myself:
idempotent ingest, backpressure, rollups that stay consistent with raw data, and alerting that
doesn't flap. ServScope speaks standard OTLP, so the shop could point at any of those
instead.

**What happens if PostgreSQL goes down?**
The shop is unaffected. Ingest answers 503 so the Collector keeps and retries batches; reads
answer 503 "Database unavailable" with Retry-After; `/readyz` fails; the dashboard keeps the
last data with a stale banner. When it comes back, nothing is lost (drill D2).

**How do you avoid double-counting when the Collector retries?**
The primary key `(trace_id, span_id)` with `ON CONFLICT DO NOTHING RETURNING` returns only
rows actually inserted, and only those update the metrics, in the same transaction.

**Two batches update the same minute at the same time. What stops lost updates or deadlocks?**
The increment happens inside the UPDATE (`count = count + excluded.count`), not
read-modify-write in Python, and rows are upserted in a fixed key order, so two transactions
can't lock them in opposite orders. Tested with concurrent batches.

**How are percentiles computed?**
From a 12-bucket histogram per minute (5 ms to 10 s), so they are estimates accurate to the
bucket width, and they merge across minutes and services. Below 20 requests, none is shown.

**Why does an alert take about a minute?**
Rules read only data at least 30 s old, then the condition must hold 30 s, then the next 15 s
evaluation sees it. Measured 69–72 s. Faster alerts would page on spans still in transit.

**How do you tell a service that is down from one that just isn't being called?**
The "unavailable" rule looks at callers' client spans tagged with `peer.service`. If callers
tried and nothing arrived, it's unreachable. If callers stopped calling, it's starved: no
alert. Found on the live stack, where the first version alerted on healthy payments when
inventory was down.

**What was the hardest bug?**
The double charge. Orders timed out at 2 s, but payments finished the charge anyway, and the
existing test passed because the in-process transport cancelled the handler, which a real
network never does. I fixed the test transport first so the bug reproduced, then added
deadline propagation and a void. 132 timed-out orders afterwards: 0 charges.

**How did you find the performance bottleneck?**
Measured first (a baseline sweep from 1 to 32 concurrent exporters showed throughput flat
from 2 exporters with ServScope's core at ~98%), then profiled at saturation with py-spy. 44%
of samples were in the ORM's bulk-insert bookkeeping. I changed only that path, re-ran the
identical sweep, and counted the stored rows to make sure the speed-up wasn't just faster
200s.

**What would you do to scale it 10×?**
More uvicorn workers or instances behind the Collector (the evaluator is already safe with
several processes: advisory lock). Then partition `spans` by time so retention drops
partitions instead of deleting rows, and move raw spans to a column store if queries need
more than 24 h. Measure each step; don't guess.

**What would you change if you started again?**
Partition spans by day from the start. Make the void durable (an outbox) instead of one
attempt. Add user accounts instead of one admin token before ever exposing it beyond
localhost.

**What's not built?**
Notifications (email/Slack), user accounts, trace sampling (metrics assume 100%), a cleanup for
reservations whose release failed. All listed in the README's known limitations.

## Numbers to remember

| What | Value | Source |
| --- | --- | --- |
| Spans per checkout | 9, across 4 services | README |
| Ingest, before → after | ~3,400 → ~17,000 spans/s, one core | performance.md |
| Export p50 at 4 exporters | 210 → 41 ms | performance.md |
| End-to-end telemetry latency | ~1.48 s p50 | performance.md |
| Read API at 30 users | p50 7 ms, p99 210 ms, 0 failures | performance.md |
| Alert fires after a failure | 69–72 s (162 s for "unavailable") | README |
| Flood drill | 113 accepted batches = exactly 113,000 spans stored | reliability.md |
| Double charge after fix | 0 charges for 132 timed-out orders | README |
| Tests | 220 Python, 45 UI unit, 5 browser end-to-end, in CI | CI |

## Resume bullets

Pick 3–4. They are written to be defended line by line with this repository.

- Built **ServScope**, a distributed-tracing and observability platform (Python, FastAPI,
  PostgreSQL, OpenTelemetry, React/TypeScript) that ingests OTLP traces and logs from a
  four-service app and links alerts to the traces showing the root cause.
- Designed **idempotent, at-least-once ingest**: batches acknowledged only after commit,
  duplicate-safe keys, and per-minute metrics built in the same transaction; verified by
  failure drills (database outage, crash mid-write, ingest flood) ending in an exact
  metrics-equals-spans check.
- **Profiled and optimised ingest ~5×** (≈3,400 → ≈17,000 spans/s on one core; p50 210 → 41 ms)
  by replacing the ORM bulk-insert path with SQLAlchemy Core after py-spy showed 44% of CPU
  there; measured with Locust, 3 runs per level, same workload before and after.
- Implemented **alerting as a tested state machine** (pending → firing → recovering) on settled
  data, with deduplication enforced by a partial unique index and "down vs starved" detection
  from callers' client spans.
- Found and fixed a **double-charge bug** with deadline propagation and compensating voids
  (0 charges across 132 timed-out orders); CI runs 220 Python tests, browser tests, scripted
  incidents, failure drills and secret/dependency scans on every push.
