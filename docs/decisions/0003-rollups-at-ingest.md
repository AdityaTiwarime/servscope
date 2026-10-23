# 0003. Build per-minute metrics at ingest, from newly inserted spans only

Status: accepted

## Context

The dashboard and the alert rules need request rate, error rate and p50/p95/p99 per service, every 15 s. Computing them from raw spans on every read would scan millions of rows.

## Decision

Each ingest batch updates one row per (service, operation, minute) with counts and a 12-bucket latency histogram, in the same transaction as the spans, using only the rows the INSERT actually inserted (`RETURNING`). Concurrent batches add inside the UPDATE, in a fixed key order.

## Consequences

- Reads of metrics are cheap (p50 4 ms for a 15-minute chart at 30 users), and a retried batch can't double-count.
- Percentiles are estimates, accurate to the bucket width; with fewer than 20 requests none is shown.
- Spans that arrive more than 15 minutes late are stored but not counted, so metrics that alerts may already have read don't change underneath them.
- Metrics count every server span, which is right only because the demo samples 100% of requests.

## Alternative considered

A periodic background job aggregating raw spans: simpler ingest, but metrics lag and a crash between the two steps can leave them disagreeing with the spans.
