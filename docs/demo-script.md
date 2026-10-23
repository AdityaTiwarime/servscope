# Demo script

A live demo in about 7 minutes: a healthy shop, an outage, finding the cause from the
evidence, and recovery. Then three minutes of "how do you know it works" for technical
interviewers. Rehearse it once end to end. Timings are from this project's measured runs and
vary by up to one evaluation cycle (15 s).

## Before the call (5 minutes)

```bash
cp .env.example .env
# in .env: SERVSCOPE_ENV=demo, SERVSCOPE_FAILURE_INJECTION=true,
#          SERVSCOPE_ADMIN_TOKEN=<openssl rand -hex 24>, SERVSCOPE_FAULT_TOKEN=<openssl rand -hex 24>
docker compose up --build -d
docker compose run --rm --no-deps smoke      # must print all checks passing
```

Let the traffic generator run for 3–4 minutes, so the charts have history. Open
http://localhost:8000 in one tab and keep the admin token ready to paste. Have the repository
open on GitHub in a second tab.

## 1. The problem (30 s)

> "When a request fails in a system of several services, the error shows up in one place and
> the cause is somewhere else. ServScope collects traces, metrics and logs from a small shop
> with four services and takes you from 'checkouts are failing' to the cause, with evidence."

## 2. A healthy system (1 min)

- **Overview**: four services healthy, front-door rate about 2 checkouts/s, error rate, p95.
  Point at the service map: gateway → orders → inventory and payments.
- **Traces**: open one checkout. Nine spans across four services, one trace ID. "Every
  service propagates the W3C `traceparent` header; ServScope stitches the spans together."

## 3. Break it (1 min)

- **Demo scenarios** → *Unlock admin* → paste the token → start **Payments timeout**.
- Say while it runs: "Payments now takes 3 s. Orders gives up after 2 s. The customer sees a
  504. Scenarios are off by default and need three locks: demo mode, injection switched on,
  and the admin token."
- Optional: in a terminal, `curl -X POST localhost:8080/checkout -H "content-type:
  application/json" -d '{"items":[{"sku":"tea","qty":1}]}'` returns 504 after about 2 s.

## 4. The alert (about 70 s after starting)

- Explain the wait honestly: "Rules only judge data at least 30 s old, so spans still in
  transit don't look like an outage. Then the condition must hold for 30 s. That's about a
  minute: a deliberate trade, no false alarms."
- **Overview** shows two alerts on the gateway (errors and latency), grouped as one problem.

## 5. Investigate (2 min)

- *Investigate* → the incident page: what the rule saw, the incident period shaded, the
  timeline, and up to 5 traces captured when it fired.
- *Open a failing trace*: the waterfall shows orders' call to payments cut at 2 s, and the
  critical path outlined. "Where the time went" says it in one sentence. The payments span
  kept running after its caller gave up.
- The span's log lines: "payments timed out after 2.0s, voiding the charge and releasing
  stock".
- The point: "I didn't start from payments' logs. I started from what customers felt (the
  gateway), and the linked traces led to the cause."

## 6. Recovery (about 2.5 min, talk through it)

- *Stop now*. Checkouts succeed again immediately; the incident resolves by itself after the
  failing minutes leave the window and the rule has been clear for 60 s.
- While waiting, the business detail: "Customers who got a 504 were never charged. Each call
  carries a deadline, and orders sends a void when it gives up. Milestone 4 found a real
  double charge here; 132 timed-out orders afterwards produced 0 charges."
- The incident timeline now shows opened → recovering → resolved, with no one closing it by
  hand.

## 7. "How do you know it works?" (3 min, technical interviewers)

Pick what the interviewer cares about:

- **Tests and CI**: on GitHub → Actions. 220 Python tests (many against a real PostgreSQL),
  45 UI tests, 5 Playwright browser tests against the running stack, the scripted incident,
  failure drills, gitleaks and dependency audits, on every push.
- **Reliability**: [reliability.md](reliability.md). "I stopped the database, killed
  ServScope mid-write and flooded ingest. Afterwards the per-minute metrics still exactly equal
  the stored spans."
- **Performance**: [performance.md](performance.md). "Ingest saturated one core at about 3,400
  spans/s. I profiled it, found 44% of CPU in the ORM, switched the hot path to SQLAlchemy
  Core: about 17,000 spans/s on the same workload, verified in the database."
- **Design**: [architecture.md](architecture.md) and the [decision records](decisions/README.md).

## If something goes wrong live

- No data on Overview: `docker compose ps`. All services should be up. `docker compose logs
  servscope | tail`.
- Scenario buttons missing: demo mode or injection is off in `.env`. Restart with
  `docker compose up -d`.
- Alert hasn't fired after 2 minutes: check *Settings → ServScope itself*. If alert evaluation
  says "stalled" or "failing", that is ServScope reporting its own problem. Show it, and say so.
- Fallback with no browser: `docker compose run --rm --no-deps incident-demo` runs the whole
  story in the terminal and prints the timeline.

## Shut down

```bash
docker compose down            # add --volumes to delete the stored data
```
