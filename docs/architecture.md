# Architecture

How ServScope is put together, in five diagrams. GitHub renders them (Mermaid). Each one
names the files where the behaviour lives.

## 1. The whole system

```mermaid
flowchart LR
    subgraph shop["Demo shop (instrumented with OpenTelemetry)"]
        gw[gateway :8080] --> ord[orders]
        ord --> inv[inventory]
        ord --> pay[payments]
    end
    traffic[traffic generator] --> gw
    shop -- "OTLP spans + logs<br/>(background export, bounded queue)" --> col[OTel Collector<br/>batch 2 s / 512, retry on 503]
    col -- "OTLP/HTTP protobuf<br/>+ ingest token" --> ss
    subgraph ss["ServScope (one FastAPI process)"]
        ing[ingest] --> db[(PostgreSQL 16)]
        api[read API] --> db
        ev[alert evaluator<br/>every 15 s] --> db
        ret[retention<br/>every minute] --> db
        ui[dashboard<br/>React, served by ServScope] --> api
    end
    ss -. "admin token, demo mode only" .-> faults["/_faults switches<br/>on orders and payments"]
```

- The shop never waits for telemetry: if the Collector or ServScope is down, checkouts still
  work (drills D1, D2, D4 in [reliability.md](reliability.md)).
- ServScope stores everything in PostgreSQL. There is no separate metrics or search engine
  ([ADR 0001](decisions/0001-postgresql-for-everything.md)).
- Code: `src/demo_services/` (the shop and its OpenTelemetry setup), `deploy/collector.yaml`,
  `src/servscope/`.

## 2. One checkout, one trace

```mermaid
sequenceDiagram
    participant C as Customer
    participant G as gateway
    participant O as orders
    participant I as inventory
    participant P as payments
    C->>G: POST /checkout
    G->>O: POST /orders (traceparent)
    O->>I: POST /reservations
    I-->>O: 201 reserved
    O->>P: POST /charges (Idempotency-Key = order ID,<br/>X-Request-Timeout-Ms = time left)
    alt charge ok
        P-->>O: 201
        O-->>G: 201 order placed
    else declined, failed or timed out (2 s)
        O->>P: POST /charges/void (on timeout)
        O->>I: release the reservation
        O-->>G: 402 / 504
    end
    G-->>C: answer
```

Nine spans per checkout: a server span in each service, a client span for each call, and the
work inside inventory and payments. Trace context crosses services only in the W3C
`traceparent` header. The deadline header and the void exist because Milestone 4 found a
double charge ([ADR 0006](decisions/0006-deadline-and-void.md)).

## 3. Ingest: from a batch to stored rows

```mermaid
sequenceDiagram
    participant Col as Collector
    participant In as ingest.py
    participant W as worker thread
    participant DB as PostgreSQL
    Col->>In: POST /v1/traces (gzip protobuf, token)
    In->>In: check token (constant time), content type, 4 MB limit
    In->>In: take a write slot (8 total), wait at most 1 s
    alt no slot within 1 s
        In-->>Col: 503 + Retry-After: 1 (Collector retries)
    else slot taken
        In->>W: unzip (size-limited) + decode + scrub secrets
        W-->>In: rows
        In->>DB: BEGIN
        In->>DB: INSERT spans ... ON CONFLICT DO NOTHING RETURNING (only new rows)
        In->>DB: UPSERT metric_rollups (+= counts, fixed key order)
        In->>DB: COMMIT
        In-->>Col: 200 (only after commit)
    end
```

- **Acknowledged means stored:** the 200 comes after the commit, so a crash mid-write means
  the Collector re-sends ([ADR 0002](decisions/0002-ack-after-commit-idempotent-ingest.md)).
- **Retries can't double-count:** `(trace_id, span_id)` is the key, and only rows that were
  actually inserted feed the per-minute metrics, in the same transaction
  ([ADR 0003](decisions/0003-rollups-at-ingest.md)).
- **Backpressure, not queues:** the slot is taken before any decoding, so refusing a flood
  costs nothing ([ADR 0004](decisions/0004-backpressure-with-503.md)).
- **SQLAlchemy Core, not the ORM, for the inserts:** about 5× the throughput on one core
  ([ADR 0007](decisions/0007-core-inserts.md), [performance.md](performance.md)).

## 4. Alerts: one state machine per rule and service

```mermaid
stateDiagram-v2
    [*] --> ok
    ok --> pending: condition true
    pending --> ok: condition false
    pending --> firing: true for the whole `for` time<br/>(opens an incident, links up to 5 traces)
    firing --> recovering: condition false
    recovering --> firing: true again (event on the same incident)
    recovering --> ok: false for the whole `recover` time<br/>(resolves the incident)
    ok --> no_data: nothing arrived
    no_data --> ok: data again
    firing --> firing: data stopped (incident stays open)
```

- `alerting.step` is a pure function, unit-tested transition by transition. The evaluator
  (`evaluator.py`) runs it every 15 s in one transaction behind a PostgreSQL advisory lock,
  and stores the state, so a restart resumes half-elapsed timers.
- Rules only look at data at least 30 s old, so spans still in transit don't look like
  silence ([ADR 0005](decisions/0005-alerts-on-settled-data.md)).
- At most one open incident per rule and service is enforced by a partial unique index, not
  only by code.

## 5. Data model

```mermaid
erDiagram
    spans {
        text trace_id PK
        text span_id PK
        text parent_span_id
        text service
        text operation
        int kind
        bigint start_ns
        bigint duration_ns
        int status_code
        jsonb attributes
    }
    logs {
        bigint id PK
        bytea fingerprint UK
        text trace_id
        text span_id
        int severity_number
        text message
    }
    metric_rollups {
        text service PK
        text operation PK
        timestamptz minute PK
        int request_count
        int error_count
        int_array latency_buckets
    }
    alert_rules ||--o{ alert_states : "one per service"
    alert_rules ||--o{ incidents : opens
    incidents ||--o{ incident_events : timeline
    spans ||--o{ logs : "trace_id, span_id"
    audit_log {
        text actor
        text action
        jsonb details
    }
```

- `metric_rollups` holds one row per (service, operation, minute) with 12 fixed latency
  buckets (5 ms to 10 s). p50/p95/p99 are estimated from them, accurate to the bucket width.
- `app_settings` holds the retention days changed from the Settings screen.
- Retention deletes in batches of 5,000 rows every minute, so ingest never waits behind one
  huge delete. Open incidents are never deleted.
