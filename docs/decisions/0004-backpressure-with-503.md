# 0004. Backpressure with bounded write slots and 503 + Retry-After

Status: accepted

## Context

An ingest flood must not take ServScope down or grow memory without limit, and refused data must not be lost.

## Decision

At most 8 batches are processed at once. A batch that waits more than 1 s for a slot gets `503 Retry-After: 1`, which the Collector retries. The slot is taken before the body is unzipped and decoded, and decoding runs in a worker thread.

## Consequences

- A refused batch costs almost no CPU, so ServScope keeps answering other requests: in the flood drill, the slowest `/healthz` went from 7.2 s to 768 ms after this ordering was fixed.
- Under overload, latency grows and some batches are refused and retried, but nothing acknowledged is lost and memory stays flat (86–95 MB at every load level in Milestone 7).

## Alternative considered

An unbounded in-memory queue: absorbs bursts, then runs out of memory under a sustained flood.
