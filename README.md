# ServScope

From an application failure to evidence-based root-cause investigation in one place.

ServScope collects traces, metrics and logs from a small multi-service app (a demo shop with
a gateway, orders, payments and inventory) and helps a developer find out why requests are
slow or failing.

> **Status: Milestone 1 of 8.** The gateway service sends traces through an OpenTelemetry
> Collector into ServScope, which stores them in PostgreSQL. The other services, the
> dashboard, alerts and failure scenarios arrive in later milestones.

## Run it

Needs Docker with Compose v2.

```bash
git clone https://github.com/AdityaTiwarime/servscope
cd servscope
cp .env.example .env          # then change the two secrets in .env
docker compose up --build
```

In a second terminal, place an order and check that its trace was stored:

```bash
curl -X POST localhost:8080/checkout -H "content-type: application/json" \
     -d '{"items":[{"sku":"tea","qty":2}]}'
docker compose run --rm smoke     # sends a checkout with a known trace ID and verifies it
```

Stop with `docker compose down` (add `--volumes` to delete the stored data).

| Port | What |
| --- | --- |
| `localhost:8080` | Demo gateway (`POST /checkout`) |
| `localhost:8000` | ServScope API (`/healthz`, `/readyz`, `/docs` in demo mode) |

Both are bound to localhost only. PostgreSQL is not published at all.

## How it fits together (so far)

```
gateway --OTLP--> OTel Collector --OTLP/HTTP + token--> ServScope --> PostgreSQL
```

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
src/demo_services/   The demo shop (gateway so far) and its OpenTelemetry setup
migrations/          Alembic database migrations
deploy/              OpenTelemetry Collector config
scripts/smoke.py     End-to-end check: checkout -> trace in PostgreSQL
tests/               Unit tests (no database) and integration tests (PostgreSQL)
```

## License

MIT
