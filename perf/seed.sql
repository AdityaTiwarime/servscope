-- Seed history for the read-API benchmark (workload W4).
--
--   psql -v hours=24 -v rps=2 -f perf/seed.sql
--
-- Generates `hours` of demo-shop history ending now, at `rps` checkouts per second, inside
-- PostgreSQL (seconds, instead of replaying hours of traffic). Each checkout is the same
-- nine-span shape the real shop produces. About 5% are declined cards (402) and 2% payment
-- timeouts (504, errors), so error filters and log searches find something. The per-minute
-- metrics are then built from these same spans with the same buckets as ingest, so every
-- number the dashboard shows agrees with the traces behind it.
--
-- It REPLACES all telemetry in the database. Benchmark databases only.

\set ON_ERROR_STOP on
TRUNCATE spans, logs, metric_rollups, incidents, incident_events, alert_states;

CREATE TEMP TABLE checkouts AS
SELECT n,
       md5('seed' || n) AS trace_id,
       now() - make_interval(hours => :hours) + make_interval(secs => n / :rps::float) AS t,
       CASE WHEN r < 0.05 THEN 'declined' WHEN r < 0.07 THEN 'timeout' ELSE 'ok' END AS outcome,
       (3 + random() * 5)::numeric(8, 3) AS inv_ms,   -- inventory work
       (2 + random() * 4)::numeric(8, 3) AS pay_ms    -- payments work
FROM generate_series(0, (:hours * 3600 * :rps)::int - 1) AS n,
     LATERAL (SELECT random() AS r) AS x;

-- One row per span: (k, parent k, service, operation, kind, start offset ms, duration ms,
-- status code, http status). Kinds follow OTLP: 1 internal, 2 server, 3 client.
CREATE TEMP TABLE shaped AS
SELECT c.trace_id, c.t, c.n, c.outcome, s.*
FROM checkouts c,
LATERAL (
  SELECT CASE c.outcome WHEN 'timeout' THEN 2000.0 ELSE c.pay_ms END AS pay
) p,
LATERAL (VALUES
  (1, NULL, 'gateway',   'POST /checkout',     2, 0.0,              c.inv_ms + p.pay + 8,
     CASE WHEN c.outcome = 'timeout' THEN 2 ELSE 0 END,
     CASE c.outcome WHEN 'ok' THEN 201 WHEN 'declined' THEN 402 ELSE 504 END),
  (2, 1,    'gateway',   'POST orders',        3, 1.0,              c.inv_ms + p.pay + 6,
     CASE WHEN c.outcome = 'timeout' THEN 2 ELSE 0 END,
     CASE c.outcome WHEN 'ok' THEN 201 WHEN 'declined' THEN 402 ELSE 504 END),
  (3, 2,    'orders',    'POST /orders',       2, 2.0,              c.inv_ms + p.pay + 4,
     CASE WHEN c.outcome = 'timeout' THEN 2 ELSE 0 END,
     CASE c.outcome WHEN 'ok' THEN 201 WHEN 'declined' THEN 402 ELSE 504 END),
  (4, 3,    'orders',    'POST inventory',     3, 2.5,              c.inv_ms + 1, 0, 201),
  (5, 4,    'inventory', 'POST /reservations', 2, 3.0,              c.inv_ms,     0, 201),
  (6, 5,    'inventory', 'reserve_stock',      1, 3.5,              c.inv_ms - 1, 0, NULL),
  (7, 3,    'orders',    'POST payments',      3, c.inv_ms + 4,     p.pay + 1,
     CASE WHEN c.outcome = 'timeout' THEN 2 ELSE 0 END, NULL),
  (8, 7,    'payments',  'POST /charges',      2, c.inv_ms + 4.5,   p.pay,        0,
     CASE c.outcome WHEN 'declined' THEN 402 WHEN 'timeout' THEN 504 ELSE 201 END),
  (9, 8,    'payments',  'authorize_card',     1, c.inv_ms + 5,     p.pay - 1,    0, NULL)
) AS s(k, parent_k, service, operation, kind, offset_ms, dur_ms, status_code, http_status);

INSERT INTO spans (trace_id, span_id, parent_span_id, service, operation, kind, start_time,
                   start_ns, duration_ns, status_code, status_message, http_status,
                   attributes, attributes_truncated, received_at)
SELECT trace_id,
       substr(md5(n || ':' || k), 1, 16),
       CASE WHEN parent_k IS NULL THEN NULL ELSE substr(md5(n || ':' || parent_k), 1, 16) END,
       service, operation, kind,
       t + make_interval(secs => offset_ms / 1000.0),
       (extract(epoch FROM t) * 1e9)::bigint + (offset_ms * 1e6)::bigint,
       (greatest(dur_ms, 0.2) * 1e6)::bigint,
       status_code,
       CASE WHEN status_code = 2 THEN 'payments timed out after 2.0s' END,
       http_status,
       jsonb_build_object('order.id', substr(trace_id, 1, 16), 'http.request.method', 'POST',
                          'demo.seed', true),
       false,
       t + interval '3 seconds'
FROM shaped;

INSERT INTO logs (fingerprint, time, time_ns, service, severity_number, severity_text,
                  trace_id, span_id, message, attributes)
SELECT decode(md5(trace_id || ':' || k || ':log'), 'hex'),
       t + make_interval(secs => offset_ms / 1000.0),
       (extract(epoch FROM t) * 1e9)::bigint + (offset_ms * 1e6)::bigint,
       service,
       CASE WHEN outcome = 'timeout' THEN 17 WHEN outcome = 'declined' THEN 13 ELSE 9 END,
       CASE WHEN outcome = 'timeout' THEN 'ERROR' WHEN outcome = 'declined' THEN 'WARN' ELSE 'INFO' END,
       trace_id,
       substr(md5(n || ':' || k), 1, 16),
       CASE
         WHEN k = 3 AND outcome = 'ok' THEN 'order ' || substr(trace_id, 1, 16) || ' placed'
         WHEN k = 3 AND outcome = 'declined' THEN 'order ' || substr(trace_id, 1, 16) || ' rejected: card declined, releasing stock'
         WHEN k = 3 THEN 'order ' || substr(trace_id, 1, 16) || ' failed: payments timed out after 2.0s, voiding the charge and releasing stock'
         WHEN k = 9 AND outcome = 'declined' THEN 'card declined for order ' || substr(trace_id, 1, 16)
         WHEN k = 9 THEN 'charged for order ' || substr(trace_id, 1, 16)
         ELSE 'reserved stock for order ' || substr(trace_id, 1, 16)
       END,
       '{}'::jsonb
FROM shaped
WHERE k IN (3, 6, 9);

-- Metrics from the spans, exactly as ingest builds them: server spans, in the minute they
-- ended, latency in the same 12 buckets (upper edges 5 ms ... 10 s, inclusive).
INSERT INTO metric_rollups (service, operation, minute, request_count, error_count, latency_buckets)
SELECT service, operation,
       date_trunc('minute', to_timestamp((start_ns + duration_ns) / 1000000000)),
       count(*),
       count(*) FILTER (WHERE status_code = 2),
       ARRAY[
         count(*) FILTER (WHERE duration_ns <= 5e6),
         count(*) FILTER (WHERE duration_ns > 5e6 AND duration_ns <= 10e6),
         count(*) FILTER (WHERE duration_ns > 10e6 AND duration_ns <= 25e6),
         count(*) FILTER (WHERE duration_ns > 25e6 AND duration_ns <= 50e6),
         count(*) FILTER (WHERE duration_ns > 50e6 AND duration_ns <= 100e6),
         count(*) FILTER (WHERE duration_ns > 100e6 AND duration_ns <= 250e6),
         count(*) FILTER (WHERE duration_ns > 250e6 AND duration_ns <= 500e6),
         count(*) FILTER (WHERE duration_ns > 500e6 AND duration_ns <= 1000e6),
         count(*) FILTER (WHERE duration_ns > 1000e6 AND duration_ns <= 2500e6),
         count(*) FILTER (WHERE duration_ns > 2500e6 AND duration_ns <= 5000e6),
         count(*) FILTER (WHERE duration_ns > 5000e6 AND duration_ns <= 10000e6),
         count(*) FILTER (WHERE duration_ns > 10000e6)
       ]::int[]
FROM spans WHERE kind = 2
GROUP BY 1, 2, 3;

ANALYZE spans; ANALYZE logs; ANALYZE metric_rollups;

SELECT 'checkouts' AS what, count(*) FROM checkouts
UNION ALL SELECT 'spans', count(*) FROM spans
UNION ALL SELECT 'logs', count(*) FROM logs
UNION ALL SELECT 'rollup rows', count(*) FROM metric_rollups;
