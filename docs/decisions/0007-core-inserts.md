# 0007. SQLAlchemy Core instead of the ORM on the ingest path

Status: accepted

## Context

In Milestone 7, ingest saturated ServScope's single CPU core at about 3,400 spans/s. py-spy at saturation showed 44% of the CPU in the ORM bulk-insert path, for rows that are only inserted and never loaded as objects.

## Decision

Run the same `INSERT ... ON CONFLICT DO NOTHING RETURNING` and the rollup upsert on a Core connection (`engine.begin()`) instead of an ORM session. Same SQL, same transaction, same tests.

## Consequences

- Same workload, same machine, 3 runs per level: about 17,000 spans/s instead of 3,400; export p50 at 4 concurrent exports 41 ms instead of 210 ms; 0% refused at 32 exporters instead of 22%.
- Verified in the database, not only from response codes: exactly as many spans stored as reported, and the rollups match.
- The ORM models are still the single definition of the tables (`Base.metadata`); only the hot path bypasses the session.

## Alternative considered

More uvicorn workers: scales across cores, but multiplies the waste instead of removing it. Still the next step for more throughput.
