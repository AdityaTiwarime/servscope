# 0002. Acknowledge a batch only after it is committed; make ingest idempotent

Status: accepted

## Context

The Collector retries a batch it didn't get a 200 for. ServScope can crash, or the database can go away, in the middle of a batch.

## Decision

Answer 200 only after the transaction commits. Make every write safe to repeat: spans are keyed by `(trace_id, span_id)` and logs by a content fingerprint, both inserted with `ON CONFLICT DO NOTHING`.

## Consequences

- At-least-once delivery from the Collector becomes exactly-once storage. Killing ServScope mid-write (drill D3) loses nothing and stores nothing twice.
- Two identical log lines from the same span in the same nanosecond are stored once (that is how a retried log batch is recognised). Accepted and documented.

## Alternative considered

Acknowledge on receipt and write from an in-memory queue: lower latency, but anything in the queue is lost on a crash, and the queue needs its own limits.
