import type {
  DependenciesResponse,
  LogsResponse,
  MetricsResponse,
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

export async function apiGet<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), { signal, headers: { Accept: "application/json" } });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "ServScope is unreachable", "check that the API is running");
  }
  if (!response.ok) {
    let title = `Request failed (${response.status})`;
    let detail = "";
    try {
      const body = (await response.json()) as { title?: unknown; detail?: unknown };
      if (typeof body.title === "string") title = body.title;
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // not JSON (a proxy's error page, say): keep the generic message
    }
    throw new ApiError(response.status, title, detail);
  }
  return (await response.json()) as T;
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
};
