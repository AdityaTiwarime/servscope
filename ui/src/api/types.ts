// Shapes returned by ServScope's read API (src/servscope/query.py). Keep the two in step.

export type Health = "healthy" | "degraded" | "down" | "no_data";

export interface Percentile {
  ms: number;
  at_least: boolean; // true when the value fell in the open-ended top bucket (> 10 s)
}

export interface Latency {
  p50: Percentile | null;
  p95: Percentile | null;
  p99: Percentile | null;
}

export interface ServiceSummary {
  service: string;
  health: Health;
  last_seen: string | null;
  requests: number;
  errors: number;
  error_rate: number | null;
  requests_per_second: number;
  latency_ms: Latency;
}

export interface ServicesResponse {
  from: string;
  to: string;
  services: ServiceSummary[];
}

export interface MetricPoint {
  t: string;
  requests: number | null;
  errors: number | null;
  error_rate: number | null;
  requests_per_second: number | null;
  latency_ms: Latency | null;
}

export interface MetricsResponse {
  service: string;
  step: "1m" | "5m" | "1h";
  points: MetricPoint[];
}

export interface Edge {
  caller: string;
  callee: string;
  calls: number;
  errors: number;
  error_rate: number;
}

export interface DependenciesResponse {
  edges: Edge[];
}

export interface TraceSummary {
  trace_id: string;
  start_time: string;
  duration_ms: number; // what the caller waited (the root span)
  trace_duration_ms: number; // first start to last end, including work after the answer
  root_service: string;
  root_operation: string;
  http_status: number | null;
  span_count: number;
  error_count: number;
  services: string[];
}

export interface TracesResponse {
  traces: TraceSummary[];
  next_cursor: string | null;
}

export interface SpanView {
  span_id: string;
  parent_span_id: string | null;
  service: string;
  operation: string;
  kind: "server" | "client" | "internal" | "producer" | "consumer" | "unspecified";
  offset_ns: number;
  duration_ns: number;
  self_ns: number;
  depth: number;
  status: "unset" | "ok" | "error";
  status_message: string | null;
  http_status: number | null;
  error_type: string | null;
  attributes: Record<string, unknown>;
  attributes_truncated: boolean;
  on_critical_path: boolean;
  starts_before_parent: boolean;
  log_count: number;
}

export interface TraceDetail {
  trace_id: string;
  start_ns: string; // a string: epoch nanoseconds don't fit in a JS number
  duration_ns: number;
  span_count: number;
  error_count: number;
  services: string[];
  critical_path: string[];
  slowest_span_id: string | null;
  spans: SpanView[];
}

export type Severity = "TRACE" | "DEBUG" | "INFO" | "WARN" | "ERROR" | "FATAL";

export interface LogLine {
  time: string;
  service: string;
  severity: string;
  message: string;
  trace_id: string | null;
  span_id: string | null;
  attributes: Record<string, unknown>;
}

export interface LogsResponse {
  logs: LogLine[];
  next_cursor: string | null;
}

// --- Milestone 5: alerts, incidents, scenarios, settings (src/servscope/alerts_api.py) ---

export type RuleKind = "error_rate" | "p95_latency" | "unavailable" | "failure_rise";
export type RuleStateName = "ok" | "pending" | "firing" | "recovering" | "no_data";

export interface RuleState {
  service: string;
  state: RuleStateName;
  since: string;
  value: number | null;
}

export interface RuleInput {
  name: string;
  kind: RuleKind;
  service: string | null; // null: every service
  threshold: number;
  window_s: number;
  for_s: number;
  recover_s: number;
  enabled: boolean;
}

export interface AlertRule extends RuleInput {
  id: number;
  description: string;
  updated_at: string;
  states: RuleState[];
}

export interface Incident {
  id: number;
  rule_id: number | null; // null once the rule is deleted; the incident keeps its name
  rule_name: string;
  kind: RuleKind;
  service: string;
  threshold: number;
  state: "open" | "resolved";
  opened_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  peak_value: number | null;
  sample_trace_ids: string[];
}

export type EventType = "opened" | "peak" | "breached_again" | "no_data" | "data_back" | "acknowledged" | "resolved";

export interface IncidentEvent {
  time: string;
  type: EventType;
  value: number | null;
  detail: string;
  sample_trace_ids: string[];
}

export interface IncidentDetail extends Incident {
  events: IncidentEvent[];
  rule: AlertRule | null;
}

export interface IncidentsResponse {
  incidents: Incident[];
  next_cursor: string | null;
}

export interface Scenario {
  name: string;
  title: string;
  target: string;
  fault: Record<string, unknown>;
  what_happens: string;
  what_to_look_for: string;
}

export interface ScenariosResponse {
  scenarios: Scenario[];
  max_duration_s: number;
  active: { name: string; target: string; remaining_s: number } | null;
  targets: Record<string, "reachable" | "unreachable">;
}

export interface RetentionDays {
  spans: number;
  logs: number;
  rollups: number;
  incidents: number;
}

export interface Config {
  env: "demo" | "production";
  admin_enabled: boolean;
  scenarios_enabled: boolean;
  alerts: { enabled: boolean; interval_s: number; settle_s: number };
  health: { degraded_error_rate: number; degraded_p95_ms: number; down_error_rate: number; no_data_after_s: number };
  latency: { bucket_edges_ms: number[]; min_samples: number };
  ingest: { max_body_bytes: number; write_slots: number; max_attributes_bytes: number };
  retention_days: RetentionDays;
}

export interface AuditEntry {
  time: string;
  actor: string;
  action: string;
  details: Record<string, unknown>;
}
