# 0006. Deadline propagation plus void, to fix the double charge

Status: accepted

## Context

Milestone 4 found that when orders gave up on a slow payments call after 2 s, payments still completed the charge, so a customer who was told the order failed was charged.

## Decision

Every call carries the time the caller will still wait (`X-Request-Timeout-Ms`, a relative budget so clocks don't have to agree). Payments stops at the deadline and charges nothing. Because the budget starts a few milliseconds late on the payments side, orders also sends a void when it gives up; a void that arrives before the charge leaves a marker so the late charge is refused.

## Consequences

- On the live stack, 132 timed-out orders produced 0 charges.
- The old test passed for the wrong reason (the in-process transport cancelled the handler, which a real network never does). The test transport was changed so the old code fails the test.
- If the void itself is lost, the charge may stand; a real system would retry it from a durable queue. Documented as a limitation.

## Alternative considered

A distributed transaction or a saga with a durable outbox: the right answer at scale, too heavy for a demo shop.
