# ServScope

From an application failure to evidence-based root-cause investigation in one place.

ServScope collects traces, metrics and logs from a small multi-service app (a demo shop with
a gateway, orders, payments and inventory) and helps a developer find out why requests are
slow or failing.

> **Status: Milestone 4 of 8.** The dashboard is live: service health, a service map built
> from real calls, failing requests, a trace waterfall that says where the time went, and
> the log lines of any step one click away. Alerts and the failure-scenario switch arrive in
> Milestone 5.

![Overview during a payments slowdown](docs/overview.png)

*Overview during a local run with payments slowed to 3 s: orders gives up at 2 s, so 42% of
checkouts fail and the orders → payments call is marked as failing.*

![Trace view](docs/trace.png)

*One of those checkouts. The summary names the step that held the customer up, the dashed
line marks when the customer got the 504, and the striped bar is payments still working
after nobody was waiting.*

## Run it

Needs Docker with Compose v2.

```bash
git clone https://github.com/AdityaTiwarime/servscope
cd servscope
cp .env.example .env          # then change the two secrets in .env
docker compose up --build
```

In a second terminal, place some orders:

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

docker compose run --rm smoke     # one checkout, checked in PostgreSQL and through the API
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
- **Backpressure instead of unbounded queues.** At most 8 batches are written at once; a batch
  that waits more than 1 s for a slot gets `503 Retry-After: 1`, which the Collector retries.
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
- **Retention:** spans and logs are kept 7 days, metrics 30 days, deleted every minute in
  batches of 5,000 rows so ingest never waits behind one huge delete.

**The dashboard**

- React + TypeScript, built into static files and served by ServScope itself, so the API and
  the page share one address: no cross-origin setup, and a strict Content-Security-Policy
  (scripts, styles, fonts and data only from ServScope; fonts are bundled, nothing loads from
  elsewhere).
- **Overview**: what needs attention, front-door rate, error rate and p95, each service's
  health and error trend, the service map, and the latest failing requests.
- **Trace view**: a waterfall with the critical path outlined, "where the time went" in one
  sentence (counting only the time the caller was actually waiting), spans that kept running
  after their caller gave up, and each span's attributes and log lines.
- **Traces, Logs, Services**: filters in the URL (shareable links), cursor paging, full-text
  log search, and per-service charts with the health thresholds drawn in.
- Every screen has loading, empty, error and stale states: if a refresh fails, the old data
  stays with a banner saying how old it is, instead of silently going stale.
- Dark by default, with a light theme; works down to phone width.

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
- The read API and the dashboard have no login, so ServScope must not be exposed beyond
  localhost or a private network. Admin authentication arrives with the admin endpoints in
  Milestone 5; until then Alerts, Demo scenarios and Settings show as "Soon".
- **Known bug, fixed in Milestone 5:** when payments answers after orders has timed out, the
  card can still be charged for an order the customer was told failed. The trace view shows
  it happening (a striped payments span after the 504).
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

CI runs all of these, then builds the Docker stack, runs the smoke test, and runs the
Playwright tests in a real browser against it.

## Layout

```
src/servscope/       ServScope: OTLP ingest, rollups, read API, trace analysis, retention
src/demo_services/   The demo shop: gateway, orders, inventory, payments + OpenTelemetry setup
migrations/          Alembic database migrations
deploy/              OpenTelemetry Collector config
scripts/smoke.py     End-to-end check: one checkout -> trace, logs, health and map via the API
ui/                  The dashboard (React + TypeScript + Vite) and its Playwright tests
tests/               Unit tests (no database) and integration tests (PostgreSQL)
```

## License

MIT
