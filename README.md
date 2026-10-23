# ServScope

From an application failure to evidence-based root-cause investigation in one place.

ServScope collects traces, metrics and logs from a small multi-service app (a demo shop with
a gateway, orders, payments and inventory) and helps a developer find out why requests are
slow or failing.

> **Status: Milestone 7 of 8.** Alerts and incidents work end to end; ServScope has been
> broken on purpose and measured against what the design promised
> ([docs/reliability.md](docs/reliability.md)); and it has been load-tested. One profiled
> change raised ingest from about 3,400 to about 17,000 spans/s on one CPU core, with export
> latency at 4 concurrent exports down from 210 to 41 ms (p50). Method, hardware and every
> number: [docs/performance.md](docs/performance.md).

![Overview during an incident](docs/overview.png)

*A payments timeout, live: two alerts on the gateway (errors and latency), grouped as one
problem, above the usual health view.*

![Incident page](docs/incident.png)

*The same incident after it resolved: what the rule saw, with the incident period shaded,
the timeline, and the traces captured when it fired.*

## Run it

Needs Docker with Compose v2.

```bash
git clone https://github.com/AdityaTiwarime/servscope
cd servscope
cp .env.example .env          # then change the two secrets in .env
docker compose up --build
```

The stack includes a traffic generator (2 checkouts a second, `TRAFFIC_RPS` in `.env`), so
the dashboard fills up by itself. To place orders by hand, in a second terminal:

```bash
# a normal order -> 201
curl -X POST localhost:8080/checkout -H "content-type: application/json" \
     -d '{"items":[{"sku":"tea","qty":2}]}'
# a declined card -> 402, and the reserved stock is released again
curl -X POST localhost:8080/checkout -H "content-type: application/json" \
     -d '{"items":[{"sku":"mug","qty":1}],"card_token":"tok_demo_declined"}'
# more than is in stock -> 409, and payments is never called
curl -X POST localhost:8080/checkout -H "content-type: application/json" \
     -d '{"items":[{"sku":"beans","qty":20},{"sku":"beans","qty":20}]}'

docker compose run --rm --no-deps smoke     # one checkout, checked in PostgreSQL and through the API
```

Then open the dashboard at **http://localhost:8000**, or use the same API it uses:

```bash
curl -s localhost:8000/api/services                      # health, rate, errors, p50/p95/p99
curl -s "localhost:8000/api/traces?status=error"         # failed requests, newest first
curl -s localhost:8000/api/traces/<trace_id>             # waterfall, critical path, slowest span
curl -s "localhost:8000/api/logs?trace_id=<trace_id>"    # that request's log lines
curl -s localhost:8000/api/dependencies                  # who calls whom, with error rates
curl -s localhost:8000/metrics                           # ServScope's own counters
```

Full parameters are on `localhost:8000/docs` in demo mode.

## Reproduce an incident

Failure scenarios are off by default. To turn them on for a local demo, set these in `.env`
and run `docker compose up --build` again:

```bash
SERVSCOPE_ENV=demo
SERVSCOPE_FAILURE_INJECTION=true
SERVSCOPE_ADMIN_TOKEN=<openssl rand -hex 24>
SERVSCOPE_FAULT_TOKEN=<openssl rand -hex 24>
```

**In the dashboard:** open *Demo scenarios*, click *Unlock admin* and paste the admin token,
start **Payments timeout**, then watch *Overview* and *Alerts*. When "Checkout errors"
fires, *Investigate* opens the incident; *Open a failing trace* shows orders giving up on
payments after 2 s, and the span's log lines say so. Click *Stop now* and the incident
resolves by itself once the error rate has been back under 10% for 60 s.

**Scripted** (this is what CI runs):

```bash
docker compose run --rm --no-deps incident-demo
# another scenario:
docker compose run --rm --no-deps incident-demo python scripts/incident_demo.py \
    --api http://servscope:8000 --scenario payments_slow
```

It checks the shop is healthy, starts the scenario, waits for the alert, follows the
incident to a failing trace and its logs, stops the scenario, waits for the incident to
resolve, prints the timeline, and fails if any step didn't happen. Measured on one local run
of each scenario (one machine, 2 checkouts/s, alerts every 15 s; your times will differ by
up to one evaluation cycle):

| Scenario | Alert that fired | Fired after start | Resolved after stop |
| --- | --- | --- | --- |
| Payments timeout | Checkout errors | 72 s | 165 s |
| Slow payments | Checkout latency (p95) | 69 s | 211 s |
| Payments returns errors | Checkout errors | 69 s | 150 s |
| Inventory unreachable | Service unavailable (inventory) | 162 s | 45 s |

Each run passed every check: one incident for the outage, linked traces showing the cause,
log lines linked to them, and recovery without anyone closing the incident by hand. During
"inventory unreachable", payments (starved of calls, not broken) raised nothing.

Why it takes about a minute to fire: the rule only looks at data at least 30 s old (spans
still in transit would look like silence), then the condition must hold for 30 s (`for`),
then the next 15 s evaluation notices. "Unavailable" needs a whole 2-minute window of
silence first, so it takes longer. Resolving takes longer on purpose: the failing
minutes must leave the 2-minute window, and then the rule must stay clear for 60 s
(`recover`), so one good minute doesn't close an incident that is still happening.

| Scenario | What it does | What should fire |
| --- | --- | --- |
| Slow payments | charges take 1.5 s longer | Checkout latency (p95) |
| Payments timeout | charges take 3 s; orders gives up at 2 s | Checkout errors (and latency) |
| Payments returns errors | 30% of charges answer 503 | Checkout errors |
| Inventory unreachable | orders' connections to inventory are refused | Service unavailable (inventory) and Checkout errors |

Stop with `docker compose down` (add `--volumes` to delete the stored data).

| Port | What |
| --- | --- |
| `localhost:8080` | Demo gateway (`POST /checkout`) |
| `localhost:8000` | ServScope: the dashboard, the API (`/api/...`), `/healthz`, `/readyz`, `/docs` in demo mode |

Both are bound to localhost only. PostgreSQL is not published at all.

## How it fits together (so far)

```
                 +--> inventory   (reserve stock)
gateway --> orders
                 +--> payments    (charge card, idempotency key = order ID)

every service --OTLP--> OTel Collector --OTLP/HTTP + token--> ServScope --> PostgreSQL
```

One checkout produces one trace of nine spans: a server span in each service, the client
spans for each call between them, and the work inside inventory and payments. Trace context
crosses services only through the W3C `traceparent` header.

**The demo shop**

- **Orders reserves stock first, then charges the card.** If the charge is declined, fails or
  times out (2 s), the reservation is released, so a failed order never keeps stock.
- **Charging is idempotent.** Payments requires an `Idempotency-Key` (the order ID); a repeat
  with the same key returns the original charge and charges once.
- **No overselling.** Inventory checks and takes stock under one lock, all items or none.
- **Fake card tokens only** (`tok_demo_*`); a card-number-shaped value is rejected, and tests
  check that no token ever appears in a span.

**The telemetry pipeline**

- **The demo app never waits for telemetry.** Spans are exported in the background with a
  bounded queue. With ServScope stopped, a checkout still completes; the Collector retries
  and delivers the spans once ServScope is back.
- **A batch is acknowledged only after it is committed.** If ServScope crashes mid-write, the
  Collector never got a 200 and sends the batch again.
- **Retries can't create duplicates.** `(trace_id, span_id)` is the primary key and inserts use
  `ON CONFLICT DO NOTHING`.
- **Backpressure instead of unbounded queues.** At most 8 batches are processed at once; a
  batch that waits more than 1 s for a slot gets `503 Retry-After: 1`, which the Collector
  retries. The slot is taken *before* the batch is unzipped and decoded, so a flood is
  turned away without costing CPU, and decoding runs in a worker thread so ServScope keeps
  answering other requests (found by the flood drill, where `/healthz` had taken 7 s).
- **Input limits.** Ingest token (compared in constant time), 4 MB body limit, the same limit
  on the *unzipped* size (so a small gzip bomb is refused), 8 KB of attributes per span.
- **Secrets are scrubbed on the way in.** Password, token and authorization fields, inline
  `password=...` pairs and card-number-shaped digits are replaced with `[redacted]` in spans
  and logs before anything is stored.

**Metrics, health and the read API**

- **Metrics are built from server spans, in the same transaction.** Each batch updates a row
  per (service, operation, minute) with request and error counts and a latency histogram.
  Only *newly inserted* spans count, so a retried batch can't double-count; concurrent
  batches add inside the `UPDATE`, in a fixed key order, so they can't overwrite each other
  or deadlock.
- **Percentiles come from histograms.** p50/p95/p99 are estimated from 12 fixed buckets
  (5 ms to 10 s), so they are accurate to the bucket width. With fewer than 20 requests no
  percentile is shown, rather than a meaningless one.
- **Late data is stored but not counted.** A span arriving more than 15 minutes after it
  ended is searchable but doesn't change metrics that alerts may already have read; it is
  counted in `servscope_spans_late_total` instead.
- **Silence is not health.** A service is `healthy`, `degraded` (5% errors or p95 at least
  1 s), `down` (50% errors), or `no_data` when nothing has arrived for 2 minutes.
- **The dependency map is computed from the spans** at query time (a server span whose
  parent belongs to another service is one call), so it is right whichever batch each span
  arrived in.
- **Paging uses cursors, not page numbers**, so new data arriving between two pages can't
  cause skipped or repeated results. Time windows are limited (24 h for spans and logs,
  30 days for metrics), page size is at most 100, and errors use `application/problem+json`.
- **Retention:** spans and logs are kept 7 days, metrics 30 days, resolved incidents and the
  audit log 90 days (an open incident is never deleted), deleted every minute in batches of
  5,000 rows so ingest never waits behind one huge delete. The first three can be changed on
  the Settings screen (with the admin token), within fixed limits.

**The dashboard**

- React + TypeScript, built into static files and served by ServScope itself, so the API and
  the page share one address: no cross-origin setup, and a strict Content-Security-Policy
  (scripts, styles, fonts and data only from ServScope; fonts are bundled, nothing loads from
  elsewhere).
- **Overview**: open alerts first, then what needs attention, front-door rate, error rate and
  p95, each service's health and error trend, the service map, and the latest failing
  requests.
- **Alerts, incident pages, Demo scenarios, Settings**: open incidents grouped by service,
  rules with each service's live state, history; an incident's timeline next to a chart of
  what the rule saw; scenario cards with their safety locks and a countdown; configuration,
  retention and the audit log.
- **Trace view**: a waterfall with the critical path outlined, "where the time went" in one
  sentence (counting only the time the caller was actually waiting), spans that kept running
  after their caller gave up, and each span's attributes and log lines.
- **Traces, Logs, Services**: filters in the URL (shareable links), cursor paging, full-text
  log search, and per-service charts with the health thresholds drawn in.
- Every screen has loading, empty, error and stale states: if a refresh fails, the old data
  stays with a banner saying how old it is, instead of silently going stale.
- Dark by default, with a light theme; works down to phone width.

**Alerts and incidents**

- **Four kinds of rule**: error rate above a share, p95 latency above a time, a service
  unavailable, and failures rising (errors per minute as a multiple of the previous half
  hour). Each has a window, a `for` time before it fires and a `recover` time before it
  resolves. The four default rules watch the gateway, i.e. what customers feel; the
  traces linked from an incident lead to the cause. ("Unavailable" watches every service.)
- **A state machine per rule and service**: ok → pending → firing → recovering → ok, plus
  no data. It is a pure function (`alerting.step`), unit-tested transition by transition,
  and its state is stored, so a restart resumes a half-elapsed timer.
- **Deduplication**: at most one *open* incident per rule and service, enforced by a partial
  unique index in PostgreSQL, not just by code. A long outage is one incident; a breach
  while recovering is an event on the same incident, not a new one. **Grouping**: one
  outage can trip several rules on the same service (errors and latency, say); the
  dashboard shows those together as one problem with several symptoms.
- **Silence is not recovery**: if data stops while an incident is open, the incident stays
  open (a "data stopped" event) until data shows the problem is gone.
- **"Unavailable" tells down from starved.** When inventory is unreachable, orders fails
  before calling payments, so payments goes silent too, while being fine. The rule looks at
  the calls other services *attempted* (their client spans, tagged with `peer.service`):
  callers tried and nothing arrived means unreachable (alert); callers stopped calling
  means starved (no data, no alert); a service nobody calls, like the gateway, alerts on
  silence alone. (Found on the live stack, where the first version alerted on payments.)
- **Evidence**: when an incident opens, up to 5 traces are linked: failing requests for error
  rules, the slowest for latency, the callers' failed calls for "unavailable". They come
  from the same settled window the rule judged, so they are complete traces.
- **One evaluator at a time**: each pass runs in one transaction behind a PostgreSQL advisory
  lock, so two ServScope processes on one database can't both open the same incident.
- **In-app only**: no email or Slack notifications (optional in the design, not built).

**Failure scenarios and their safety locks**

- Off by default, and three locks must all be open: `SERVSCOPE_ENV=demo`,
  `SERVSCOPE_FAILURE_INJECTION=true`, and the admin token on every start and stop. Without the
  first two the scenario endpoints are not registered at all (404). With
  `SERVSCOPE_ENV=production`, ServScope refuses to start if injection is switched on.
- The faults are real behaviour in the demo shop, switched through a `/_faults` endpoint on
  payments and orders. That endpoint exists only when the service has `DEMO_FAULT_TOKEN`,
  answers only to that token, and is left out of tracing. "Inventory unreachable" points
  orders at a closed port, so the connection is genuinely refused.
- Every scenario has a duration of at most 10 minutes, counted by the demo service itself, so
  it ends even if ServScope crashes. Starting one stops any other. Each start and stop is in
  the audit log.
- In Compose, ServScope reaches the two switches over a separate `control` network; the
  shop still reaches ServScope only through the Collector.

**Admin access**

- One admin token (`SERVSCOPE_ADMIN_TOKEN`, at least 16 characters) for every change: alert
  rules, retention, acknowledging incidents, scenarios. It travels in the `Authorization`
  header, never a cookie (so another website can't make the browser send it), and is
  compared in constant time. No token configured: every admin call is refused. After 10
  wrong tokens from one address in a minute, that address is locked out for the rest of it.
- The dashboard keeps the token in memory only, not in browser storage; reloading the page
  locks it again. Reading stays open, like the rest of the read API.
- Every change is written to the audit log in the same transaction as the change: who (and
  from which address), what, when. Reading the audit log needs the token.

**The double charge, fixed**

Milestone 4 found that when orders gave up on a slow payments call, payments still charged
the card for an order the customer was told failed. Two changes close it:

1. **Deadline propagation.** Every call carries how long the caller will still wait
   (`X-Request-Timeout-Ms`, a relative budget, so the two clocks don't have to agree).
   Payments stops working on a charge when the budget runs out and charges nothing.
2. **Void.** The budget starts when payments *receives* the call, a few milliseconds after
   orders started its timer, so a charge could still land in that gap. So orders also sends
   `POST /charges/void` when it gives up. A void reverses a completed charge; a void that
   arrives first leaves a marker, and the late charge is refused. Either order: no money.

The old test for this passed for the wrong reason: the in-process test transport cancelled
payments' handler when orders timed out, which a real network never does. The test
transport now runs each call independently of its caller, and the old code fails the new
test (1 charge after a 504); the fixed code passes (0). On the live stack, 132 timed-out
orders in one run produced 0 charges: all 132 were abandoned at the deadline.

**When things fail (Milestone 6)**

- **Errors never leak.** Every failure answers `application/problem+json` with a request ID:
  the database down is `503 Database unavailable` with Retry-After; a query cut off by the
  5 s statement timeout is `503 Query took too long`; anything else is a plain `500 Internal
  error`. The details go to ServScope's own log under the same request ID.
- **No other website can read the API.** There are no CORS headers at all: the dashboard
  comes from ServScope itself, so no other origin is ever needed. Every response also
  carries `nosniff`, `X-Frame-Options: DENY` and, for the API, `Cache-Control: no-store`.
- **ServScope watches itself.** `/api/self` and the "ServScope itself" panel on Settings show
  ingest counts and refusals, database latency and connections, and whether the alert and
  retention jobs are still running. A job that silently died shows as "stalled", because a
  dead alert evaluator would otherwise just look like a quiet day.
- **Failure drills** (`scripts/failure_drills.py`, run in CI) stop, kill and flood the running
  stack and check the outcome, ending with an exact check that the per-minute metrics still
  equal the stored spans. CI also scans the whole git history for secrets (gitleaks) and
  audits every pinned dependency (pip-audit, npm audit).

**How calls between services are recorded**

The demo services create their own client span for every call (`Downstream` in
`src/demo_services/telemetry.py`) instead of using the generic httpx instrumentation. That
instrumentation ends a span cut off by `asyncio.timeout` without marking an error, so a
timed-out call looked like success from the caller's side. Now a timeout, a connection
failure or a 5xx is an error with `error.type`, and the dependency map counts a call as
failed if either side says so. A 4xx (declined card, out of stock) is deliberately *not* an
error: the other service answered correctly. (OpenTelemetry's convention suggests marking
client 4xx as errors; this project keeps "refused" apart from "broken".)

## Known limitations

- Spans a service produces while the Collector is unreachable are retried for about 5 s and
  then dropped. That is deliberate (the shop never blocks or queues without limit for
  telemetry), but it means a trace can be incomplete if the Collector restarts.
- Demo state (stock, charges) lives in memory and resets when a service restarts.
- A reservation whose release fails (inventory unreachable at that moment) stays reserved;
  a periodic cleanup of stale reservations is not built.
- Reading (the dashboard and the read API) needs no login, so ServScope must not be exposed
  beyond localhost or a private network. There is one admin token, not user accounts.
- Alerts work on per-minute metrics, so windows are rounded to whole minutes, and an alert
  can't fire sooner than the settle delay plus its `for` time (about a minute by default).
- One void is sent per timed-out charge. If that void is also lost (payments unreachable at
  that exact moment), the charge may stand; a real system would retry it from a durable
  queue. The orders log says so when it happens.
- Telling "unavailable" from "starved" relies on callers tagging their calls with
  `peer.service`, which the demo services do. Uninstrumented callers can't be seen.
- Grouping several rules' incidents for one service happens in the dashboard only; each
  incident is still stored and resolved on its own.
- Sample traces are linked when an incident opens (and when it breaches again), not
  continuously.
- Metrics count every server span, which is right because the demo samples 100% of
  requests. With trace sampling, the counts would undercount real traffic.
- Two identical log lines from the same span in the same nanosecond are stored once (that is
  how a retried log batch is recognised).

## Develop

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and a PostgreSQL 16 with a
`servscope_test` database.

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy
TEST_DATABASE_URL=postgresql+asyncpg://servscope:servscope@localhost:5432/servscope_test \
  uv run pytest
```

Dashboard (Node 22):

```bash
cd ui
npm ci
npm run dev            # http://localhost:5173, using the API on :8000
npm run lint && npm run typecheck && npm test
npx playwright test    # needs the stack running (docker compose up)
```

CI runs all of these, plus a secret scan and a dependency audit, then builds the Docker
stack with failure scenarios on and runs, in order: the smoke test, the scripted incident
(`incident-demo`), the Playwright tests in a real browser (including the alert lifecycle),
and the failure drills.

A local stack without Docker (as used while developing) is the same processes started by
hand: `python -m servscope.app`, the Collector, the four `python -m demo_services.<name>`,
and `python scripts/traffic.py`.

## Layout

```
src/servscope/       ServScope: OTLP ingest, rollups, read API, alerts, scenarios, retention
src/demo_services/   The demo shop: gateway, orders, inventory, payments + OpenTelemetry setup
migrations/          Alembic database migrations
deploy/              OpenTelemetry Collector config
scripts/smoke.py     End-to-end check: one checkout -> trace, logs, health and map via the API
scripts/traffic.py   Steady demo traffic (the shop's customers)
scripts/incident_demo.py  The scripted incident investigation, run in CI
scripts/failure_drills.py  Breaks the running stack on purpose and checks what happens
docs/reliability.md  Every failure and threat from the design, its test, and the measured result
docs/performance.md  Load tests: method, hardware, results before and after the optimisation
perf/                Locust workloads, the benchmark runner, seed data and raw results (JSON)
ui/                  The dashboard (React + TypeScript + Vite) and its Playwright tests
tests/               Unit tests (no database) and integration tests (PostgreSQL)
```

## License

MIT
