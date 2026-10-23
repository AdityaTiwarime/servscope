# Design decisions

Short records of the choices that shape ServScope: the context, the decision, and what it cost.

1. [PostgreSQL for traces, logs and metrics](0001-postgresql-for-everything.md)
2. [Acknowledge a batch only after it is committed; make ingest idempotent](0002-ack-after-commit-idempotent-ingest.md)
3. [Build per-minute metrics at ingest, from newly inserted spans only](0003-rollups-at-ingest.md)
4. [Backpressure with bounded write slots and 503 + Retry-After](0004-backpressure-with-503.md)
5. [Alerts judge only settled data, with `for` and `recover` times](0005-alerts-on-settled-data.md)
6. [Deadline propagation plus void, to fix the double charge](0006-deadline-and-void.md)
7. [SQLAlchemy Core instead of the ORM on the ingest path](0007-core-inserts.md)
