# 0001. PostgreSQL for traces, logs and metrics

Status: accepted

## Context

ServScope needs to store spans, logs, per-minute metrics, alert state, incidents and an audit log, and the whole stack must start with one `docker compose up` on a laptop.

## Decision

Store everything in one PostgreSQL 16 database. Spans and logs are tables with indexes for every list query; metrics are pre-aggregated rows (ADR 0003); log search uses PostgreSQL full-text search.

## Consequences

- One database to run, back up and reason about. Spans and their metrics commit in the same transaction, which is what makes "metrics always equal the stored spans" checkable (drill invariant).
- Constraints do real work: the primary key deduplicates retried spans, and a partial unique index allows only one open incident per rule and service.
- It will not scale like a column store (ClickHouse) or a search engine. Time windows are limited (24 h for spans and logs, 30 days for metrics) and a statement timeout (5 s) caps any query. At the measured load (about 17,000 spans/s ingest, reads p99 210 ms at 30 users over 1.5 M spans) PostgreSQL was not the bottleneck: ServScope's own CPU was.

## Alternative considered

ClickHouse or Elasticsearch: faster at scale, but a second or third stateful system for a single-node demo, and no shared transaction with the metrics.
