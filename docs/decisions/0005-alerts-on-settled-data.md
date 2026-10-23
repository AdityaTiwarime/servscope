# 0005. Alerts judge only settled data, with `for` and `recover` times

Status: accepted

## Context

Spans reach ServScope about 1.5 s after a request ends (measured, W3), and later when the Collector retries. A rule reading the last few seconds would see missing data as silence or as a drop in errors.

## Decision

Rules evaluate only data at least 30 s old. A condition must hold for `for` (30 s) before firing and stay clear for `recover` (60 s) before resolving. 'Unavailable' compares the calls other services attempted with what arrived, to tell a service that is down from one that is merely not being called.

## Consequences

- No false alarms from spans in transit; one incident per outage instead of flapping.
- An alert fires about a minute after a failure starts (measured: 69–72 s, 162 s for 'unavailable'). That delay is the price, and it is documented.
- If data stops while an incident is open, the incident stays open: silence is not recovery.

## Alternative considered

Evaluate the most recent data: faster, but alerts on transit delay and resolves incidents during an outage that stopped all telemetry.
