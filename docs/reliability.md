# Reliability and security results

Milestone 6 turned every row of the design's failure-scenario table (section 10) and threat
model (section 9) into a test, then ran them. This file records what was tested, how, and
what happened. Where something failed, it was fixed and a regression test was added
([below](#defects-found-and-fixed)).

**Where the numbers come from.** The drill results are one local run on 2026-10-22: one
machine, the stack started without Docker (ServScope, the OpenTelemetry Collector 0.149.0,
the four demo services, PostgreSQL 16), with the built-in traffic generator sending 2
checkouts a second throughout. Latencies are from that machine and will differ elsewhere.
CI runs the same drills against the Docker Compose stack on every push; its results are
kept as the `failure-drills` artifact of each run.

## How to reproduce

```bash
uv run pytest                                   # unit + integration tests (needs PostgreSQL)
docker compose up --build -d
docker compose run --rm --no-deps incident-demo # a real incident, from alert to recovery
SERVSCOPE_INGEST_TOKEN=<from .env> \
  uv run python scripts/failure_drills.py --compose   # the six failure drills below
```

The drills break the running stack on purpose (`docker compose stop`, `kill -s SIGKILL`,
a flood of telemetry), so run them on a demo stack, never one you care about.

**The invariant.** After all the drills, one check runs over everything stored: per
service, operation and minute, the metric counts must *exactly* equal what the stored spans
say. A span lost in a crash would make the metrics disagree; a retried batch counted twice
would make them bigger. Result: **0 rows disagree.**

## Failure scenarios (design section 10)

### In the demo app: what ServScope must show

| Scenario | Expected | Tested by | Result |
| --- | --- | --- | --- |
| Slow payments | p95 rises; the waterfall shows the long payments span; the latency alert fires | `incident_demo.py --scenario payments_slow` | Alert after 69 s; linked trace's slowest span: payments, 1,501 ms; resolved 211 s after the fix |
| Payments timeout | 504s; error spans with timeout status; error-rate alert; logs one click away | `incident_demo.py` (runs in CI on every push), Playwright alert test | Alert after 72 s; orders span says `payments_timeout`; logs linked; resolved 165 s after the fix; 0 cards charged for 132 timed-out orders |
| Payments returns errors | ~30% failures; orders → payments edge red; error-rate alert | `incident_demo.py --scenario payments_errors` | Alert after 69 s; resolved 150 s after the fix |
| Inventory is down | Orders fails fast; inventory no data, then down; timeline records recovery | Drill D6 (inventory really stopped) and `incident_demo.py --scenario inventory_unreachable` | D6: 10 of 10 checkouts answered 503 within 12.7 ms, trace names `inventory_unavailable` / `ConnectError`, checkouts succeed again after restart. Scenario: "Service unavailable" after 162 s, resolved 45 s after the fix, payments (starved, not broken) raised nothing |

### In ServScope itself: what must not happen

| Failure | Expected | Tested by | Result |
| --- | --- | --- | --- |
| ServScope API stopped | Demo keeps serving at normal speed; the Collector queues and retries | Drill D1 (N1) | Checkout p95 12.5 ms with ServScope, 12.8 ms without; every checkout got its normal answer; ServScope ready 1 s after restart; all 40 traces from the outage stored complete within 2.5 s |
| Same batch delivered twice | Stored once; metrics unchanged by the second delivery | `test_same_batch_twice_is_stored_once`, `test_retried_batch_does_not_double_count`, `test_retried_log_batch_is_stored_once`, the invariant | Pass |
| ServScope restarts mid-batch | A batch is acknowledged only after commit, so the Collector re-sends it; committed rows absorbed by the unique key | Drill D3 (SIGKILL under traffic, no clean shutdown) | 40 of 40 traces stored complete; invariant 0 |
| No telemetry from a service | No data after 2 minutes, never healthy; alerts don't resolve on silence | `test_silence_is_no_data_not_healthy`, `test_silence_keeps_the_incident_open`, `test_silence_never_resolves_an_open_incident`, `test_a_starved_service_is_not_called_unavailable` | Pass |
| Clock skew between services | Each span keeps its own times; a child starting before its parent is flagged, not reordered | `test_child_starting_before_parent_is_flagged_as_clock_skew` | Pass |

### Extra drills (not in the design's table, added because the prompt asks)

| Drill | What it checks | Result |
| --- | --- | --- |
| D2 database stopped | Demo unaffected; ingest answers 503 so the Collector retries; the read API says "Database unavailable" (problem+json, no stack trace); `/readyz` 503; `/api/self` still answers; nothing lost afterwards | All checks pass; 20 refused batches counted; all 20 traces from the outage stored within 4.3 s of the database returning |
| D4 Collector stopped | Demo unaffected (the pipeline can fail without taking the app down) | Checkout p95 12.1 ms with, 14.3 ms without; all checkouts succeeded. Traces from the outage that arrived: 30 of 30 here (the outage was short enough for the services' exporters to retry); a longer outage would lose spans, see limitations |
| D5 ingest flood (N2) | Only 200 or 503; every 503 has Retry-After; acknowledged means stored, exactly; every refusal counted; ServScope keeps answering | 150 batches x 1,000 spans, 48 at once: 113 accepted, 37 refused, exactly 113,000 spans stored, slowest `/healthz` 768 ms during the flood |

## Threats (design section 9)

| Threat | Defence | Tested by |
| --- | --- | --- |
| Failure injection used outside a demo | Three locks: `SERVSCOPE_ENV=demo` and `SERVSCOPE_FAILURE_INJECTION=true` (or the routes don't exist), and the admin token; production with injection on refuses to start; 10-minute cap enforced by the demo service's own clock; audit log | `test_production_refuses_to_start_with_failure_injection`, `test_scenario_routes_do_not_exist_unless_both_locks_are_open`, `test_starting_a_scenario_needs_the_admin_token`, `test_scenarios_are_capped_at_ten_minutes`, `test_fault_switch_needs_the_token_and_switches_itself_off`, Playwright "scenario controls stay locked" |
| Fake or flooded telemetry | Ingest token (constant-time compare); 4 MB body limit, also on the unzipped size; write slots with 503 + Retry-After; overload shed *before* decoding; attribute size caps | `test_wrong_or_missing_token_is_refused`, `test_oversized_body_is_refused`, `test_gzip_bomb_is_refused`, `test_busy_write_slots_answer_503_with_retry_after`, `test_a_refused_batch_costs_no_decoding`, `test_oversized_attributes_drop_the_largest_keys_first`, drill D5 |
| Secrets or card data in logs | Scrubber for known secret keys and card-number-shaped digits, before storage; the demo only uses fake card tokens | `test_secrets_never_reach_the_database`, `test_secret_attribute_keys_are_redacted_at_any_depth`, `test_no_card_token_ever_appears_in_spans`, `..._in_logs` |
| SQL injection through filters | Bound parameters only; inputs validated | `test_sql_injection_in_filters_is_just_text`, `test_invalid_rules_are_refused` |
| Script injection in the dashboard | React escapes text; `dangerouslySetInnerHTML` forbidden by ESLint; strict Content-Security-Policy | Component test "shows HTML in span names, attributes and logs as text"; `test_index_is_served_with_security_headers` |
| Another website calling the API | No CORS headers at all: the dashboard is served from ServScope itself, so no other origin is allowed. Admin token in a header, never a cookie | `test_other_websites_cannot_read_the_api`, `test_every_response_carries_baseline_security_headers` |
| Guessing the admin token | 10 wrong tokens from one address in 60 s → 429 for the rest of the window, even with the right token; memory for this bounded | `test_guessing_the_admin_token_is_locked_out`, `test_tracking_failed_attempts_has_bounded_memory` |
| Leaked credentials | Settings from environment variables only; `.env` git-ignored; CI runs gitleaks over the whole history; the admin token never leaves the browser tab's memory | CI `security` job; `test_config_never_contains_secrets`; component test "the token lives in memory only" |
| Vulnerable dependencies | Every version pinned; CI audits them | CI `security` job: `pip-audit` (all 51 pinned Python packages, by hash) and `npm audit`, both clean on 2026-10-22 |
| Huge queries slowing the database | Time-window limits, page-size limits, a 5 s statement timeout that answers "Query took too long" (503) | `test_bad_queries_are_refused_with_problem_details`, `test_a_runaway_query_is_cut_off_with_a_clear_answer` (a real `pg_sleep`, cancelled by PostgreSQL) |
| Internal errors leaking details | One last-resort handler: problem+json with a request ID, never the exception text | `test_an_unexpected_error_leaks_nothing` |

## Defects found and fixed

Each was found by a test or drill written for this milestone, and each has a test that fails
without the fix.

1. **The read API answered a bare "500 Internal Server Error" when the database was down**,
   instead of the problem+json the design promises. Now: 503 "Database unavailable" with
   Retry-After and a request ID; anything else unexpected is a 500 problem with no details.
2. **A query cut off by the statement timeout had no clear answer.** Now 503 "Query took too
   long". The timeout is a setting (`SERVSCOPE_DB_STATEMENT_TIMEOUT_MS`), so it can be tested
   with a real slow query.
3. **An ingest flood made ServScope unresponsive** (`/healthz` took 7.2 s). Batches were
   unzipped and decoded before a write slot was taken, on the event loop, so a flood decoded
   everything, even what it was about to refuse. Now the slot comes first (a batch over
   capacity costs no CPU) and decoding runs in a worker thread.
4. **Each 1,000-span batch cost about 226 ms of CPU on the event loop** just to build one giant
   `INSERT ... VALUES` statement. As an executemany (the statement compiled once and cached),
   it costs about 18 ms. Measured with a small benchmark against PostgreSQL; median of 5 runs.
5. **Nothing limited guessing the admin token.** Now a lockout per address.
6. **The self-health panel showed a count of 39 as "39.0"** (a formatter meant for rates).

Fixes 3 and 4 together, on the same flood (150 x 1,000 spans, 48 at once), one run each:

| | Flood took | Accepted / refused | Slowest `/healthz` |
| --- | --- | --- | --- |
| Before | 25.4 s | 72 / 78 | 7,220 ms |
| Slot before decoding (fix 3) | 17.7 s | 54 / 96 | 5,504 ms |
| And executemany inserts (fix 4) | 5.5 s | 113 / 37 | 768 ms |

These are single runs to confirm a fix, not benchmarks; Milestone 7 measures performance
properly (repeated runs, fixed workloads, stated hardware).

Two problems in the drill harness itself also showed up, and are recorded so the results
can be trusted: the local stand-in for `docker compose stop` (which used `fuser -k`) also
killed every process *connected* to the port, taking the demo services down with the
Collector; and the drills bought tea faster than inventory restocks, so checkouts were
answered "409 out of stock". The drills now stop exactly one process, rotate products, and
count 409 as the shop's normal answer.

## The prompt's twelve engineering questions

| Question | Answer, and the evidence |
| --- | --- |
| Concurrent ingestion | Batches are written concurrently, up to 8 at once; per-minute counts are added inside the database's UPDATE, in a fixed key order, so they can't overwrite each other or deadlock. `test_concurrent_batches_count_exactly`, `test_concurrent_batches_are_all_stored` |
| Backpressure | A bounded number of write slots; a batch that waits over 1 s gets 503 + Retry-After and the Collector retries; refused before any CPU is spent. Drill D5 |
| Failure isolation | The demo app exports telemetry in the background with bounded queues and never waits for it. Drills D1, D2, D4: checkout latency unchanged with ServScope, its database or the Collector down |
| Persistence | Database down: ingest answers 503 (retried by the Collector), reads answer 503 problem+json, `/readyz` fails, nothing is lost once it is back. Drill D2 |
| Retries | Safe: ingest (unique key, only new rows counted), reads, stopping scenarios, voids. Not blindly retried: a checkout, so payments uses idempotency keys plus deadline and void. Drills D1, D3 and the invariant; `test_a_timed_out_order_is_never_charged` |
| Time handling | All times UTC with nanoseconds; a span counts in the minute it ended; spans over 15 minutes late are stored but not counted; alerts judge only data 30 s old. `test_late_spans_are_stored_but_not_counted`, `test_evidence_comes_from_settled_data` |
| Query performance | Indexes for every list query, keyset paging, time-window limits, a 5 s statement timeout. Measured properly in Milestone 7 |
| Resource limits | Body size (and unzipped size), attribute size, page size, time windows, write slots, statement timeout, 10-minute scenarios, bounded lockout memory. Tests listed above |
| Security | See the threat table above |
| Operational visibility | `/metrics` (Prometheus format), `/api/self` and the "ServScope itself" panel on Settings: ingest counters, refusals, database latency, connection pool, and whether the alert and retention jobs are still running (a job with no success for three intervals shows "stalled") |
| Graceful degradation | Database down: `/healthz` still answers, `/api/self` reports the database unreachable, the dashboard keeps showing the last data with a "stale" banner, alerts resume when it is back. Drill D2 |
| Recovery | Alert timers are stored, so a restart resumes them; batches are acknowledged only after commit, so a crash loses nothing the Collector won't re-send. `test_state_survives_a_restart`, drill D3 |

## Limitations

- **Telemetry produced while the Collector is down can be lost.** The demo services retry an
  export for a few seconds and then drop it, on purpose: the shop must never block or queue
  without limit for its telemetry. Drill D4's outage was short enough that nothing was lost;
  a longer one would lose spans. Once spans reach the Collector, its queue and retries
  (up to 5 minutes) cover ServScope or database outages, as drills D1 and D2 show.
- The lockout on admin-token guessing is per process and per TCP peer address. Behind a
  reverse proxy every client would share the proxy's address.
- The drills check what they can see from outside (answers, stored rows, counts). They do not
  measure memory use during the flood; ServScope's memory bound comes from the write slots
  and the body limit, not from a measurement.
- No external security review has been done. These tests show the defences listed above work
  as described; they don't show there is nothing else to defend against.
