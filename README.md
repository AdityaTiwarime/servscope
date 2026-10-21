# ServScope

From an application failure to evidence-based root-cause investigation in one place.

ServScope collects traces, metrics and logs from a small multi-service app (a demo shop with
a gateway, orders, payments and inventory) and helps a developer find out why requests are
slow or failing.

> **Status: Milestone 2 of 8.** A checkout travels through four demo services (gateway,
> orders, inventory, payments), and the whole request is stored in ServScope as one trace.
> Logs, metrics, the dashboard, alerts and failure scenarios arrive in later milestones.

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

docker compose run --rm smoke     # one checkout with a known trace ID, checked in PostgreSQL
```

Stop with `docker compose down` (add `--volumes` to delete the stored data).

| Port | What |
| --- | --- |
| `localhost:8080` | Demo gateway (`POST /checkout`) |
| `localhost:8000` | ServScope API (`/healthz`, `/readyz`, `/docs` in demo mode) |

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

## Known limitations

- Spans a service produces while the Collector is unreachable are retried for about 5 s and
  then dropped. That is deliberate (the shop never blocks or queues without limit for
  telemetry), but it means a trace can be incomplete if the Collector restarts.
- Demo state (stock, charges) lives in memory and resets when a service restarts.
- A reservation whose release fails (inventory unreachable at that moment) stays reserved;
  a periodic cleanup of stale reservations is not built.

## Develop

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and a PostgreSQL 16 with a
`servscope_test` database.

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy
TEST_DATABASE_URL=postgresql+asyncpg://servscope:servscope@localhost:5432/servscope_test \
  uv run pytest
```

CI runs the same checks, then builds the Docker stack and runs the smoke test.

## Layout

```
src/servscope/       ServScope API: config, OTLP decoding, ingest, database model
src/demo_services/   The demo shop: gateway, orders, inventory, payments + OpenTelemetry setup
migrations/          Alembic database migrations
deploy/              OpenTelemetry Collector config
scripts/smoke.py     End-to-end check: one checkout -> a four-service trace in PostgreSQL
tests/               Unit tests (no database) and integration tests (PostgreSQL)
```

## License

MIT
