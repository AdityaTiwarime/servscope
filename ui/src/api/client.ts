import type {
  AlertRule,
  AuditEntry,
  Config,
  DependenciesResponse,
  IncidentDetail,
  IncidentsResponse,
  LogsResponse,
  MetricsResponse,
  RetentionDays,
  RuleInput,
  ScenariosResponse,
  ServicesResponse,
  TraceDetail,
  TracesResponse,
} from "./types";

/** An API failure with the server's own explanation (application/problem+json). */
export class ApiError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail: string;

  constructor(status: number, title: string, detail = "") {
    super(detail ? `${title}: ${detail}` : title);
    this.status = status;
    this.title = title;
    this.detail = detail;
  }
}

type Params = Record<string, string | number | undefined | null>;

export function buildUrl(path: string, params: Params = {}): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") qs.set(key, String(value));
  }
  const query = qs.toString();
  return query ? `${path}?${query}` : path;
}

async function problemOf(response: Response): Promise<ApiError> {
  let title = `Request failed (${response.status})`;
  let detail = "";
  try {
    const body = (await response.json()) as { title?: unknown; detail?: unknown };
    if (typeof body.title === "string") title = body.title;
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // not JSON (a proxy's error page, say): keep the generic message
  }
  return new ApiError(response.status, title, detail);
}

async function call<T>(url: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "ServScope is unreachable", "check that the API is running");
  }
  if (!response.ok) throw await problemOf(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function apiGet<T>(path: string, params: Params = {}, signal?: AbortSignal, token?: string): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return call<T>(buildUrl(path, params), { signal, headers });
}

/** A change. The admin token goes in a header, never a cookie, so other sites can't send it. */
export function apiSend<T>(method: "POST" | "PUT" | "DELETE", path: string, token: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return call<T>(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
}

export interface Window {
  from: string;
  to: string;
}

export const api = {
  services: (w: Window, signal?: AbortSignal) => apiGet<ServicesResponse>("/api/services", { ...w }, signal),
  metrics: (service: string, w: Window, step: string, signal?: AbortSignal) =>
    apiGet<MetricsResponse>(`/api/services/${encodeURIComponent(service)}/metrics`, { ...w, step }, signal),
  dependencies: (w: Window, signal?: AbortSignal) => apiGet<DependenciesResponse>("/api/dependencies", { ...w }, signal),
  traces: (params: Params, signal?: AbortSignal) => apiGet<TracesResponse>("/api/traces", params, signal),
  trace: (traceId: string, signal?: AbortSignal) => apiGet<TraceDetail>(`/api/traces/${encodeURIComponent(traceId)}`, {}, signal),
  logs: (params: Params, signal?: AbortSignal) => apiGet<LogsResponse>("/api/logs", params, signal),
  config: (signal?: AbortSignal) => apiGet<Config>("/api/config", {}, signal),
  rules: (signal?: AbortSignal) => apiGet<{ rules: AlertRule[] }>("/api/alert-rules", {}, signal),
  incidents: (params: Params, signal?: AbortSignal) => apiGet<IncidentsResponse>("/api/incidents", params, signal),
  incident: (id: number, signal?: AbortSignal) => apiGet<IncidentDetail>(`/api/incidents/${id}`, {}, signal),
  scenarios: (signal?: AbortSignal) => apiGet<ScenariosResponse>("/api/scenarios", {}, signal),
  audit: (token: string, signal?: AbortSignal) => apiGet<{ entries: AuditEntry[] }>("/api/audit", { limit: 50 }, signal, token),
};

/** Everything that needs the admin token. */
export const admin = {
  check: (token: string) => apiGet<{ entries: AuditEntry[] }>("/api/audit", { limit: 1 }, undefined, token),
  createRule: (token: string, rule: RuleInput) => apiSend<AlertRule>("POST", "/api/alert-rules", token, rule),
  updateRule: (token: string, id: number, rule: RuleInput) => apiSend<AlertRule>("PUT", `/api/alert-rules/${id}`, token, rule),
  deleteRule: (token: string, id: number) => apiSend<undefined>("DELETE", `/api/alert-rules/${id}`, token),
  acknowledge: (token: string, id: number) => apiSend<IncidentDetail>("POST", `/api/incidents/${id}/ack`, token),
  startScenario: (token: string, name: string, durationS: number) =>
    apiSend<ScenariosResponse>("POST", `/api/scenarios/${encodeURIComponent(name)}/start`, token, { duration_s: durationS }),
  stopScenarios: (token: string) => apiSend<ScenariosResponse>("POST", "/api/scenarios/stop", token),
  saveRetention: (token: string, days: Omit<RetentionDays, "incidents">) =>
    apiSend<RetentionDays>("PUT", "/api/settings/retention", token, days),
};
