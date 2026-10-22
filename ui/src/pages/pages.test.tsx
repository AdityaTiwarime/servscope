import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SpanView, TraceDetail as Trace } from "../api/types";
import { ServiceMap } from "../components/ServiceMap";
import { Overview } from "./Overview";
import { TraceDetail } from "./TraceDetail";

const MS = 1_000_000;
const TRACE_ID = "4f2a91c7e05b4d1a9c33a2f8d6b07e15";

type Route_ = [RegExp, (url: URL) => Response | Promise<Response>];

function mockApi(routes: Route_[]) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://test");
    calls.push(url.pathname + url.search);
    for (const [pattern, answer] of routes) if (pattern.test(url.pathname)) return answer(url);
    return new Response(JSON.stringify({ title: "Not found", status: 404 }), { status: 404 });
  });
  return calls;
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function renderAt(path: string, element: React.ReactNode, routePath: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function span(id: string, parent: string | null, service: string, op: string, offMs: number, durMs: number, extra: Partial<SpanView> = {}): SpanView {
  return {
    span_id: id, parent_span_id: parent, service, operation: op, kind: "server", offset_ns: offMs * MS,
    duration_ns: durMs * MS, self_ns: durMs * MS, depth: 0, status: "ok", status_message: null, http_status: null,
    error_type: null, attributes: {}, attributes_truncated: false, on_critical_path: false, starts_before_parent: false,
    log_count: 0, ...extra,
  };
}

const TRACE: Trace = {
  trace_id: TRACE_ID, start_ns: "1761123456000000000", duration_ns: 3000 * MS, span_count: 3, error_count: 1,
  services: ["gateway", "orders", "payments"], critical_path: ["gw", "od", "pay"], slowest_span_id: "pay",
  spans: [
    span("gw", null, "gateway", "POST /checkout", 0, 2040, { self_ns: 10 * MS, http_status: 504, status: "error", log_count: 1 }),
    span("od", "gw", "orders", "POST /orders", 5, 2030, { depth: 1, self_ns: 20 * MS, status: "error", error_type: "payments_timeout", log_count: 2 }),
    span("pay", "od", "payments", "POST /charges", 15, 2985, { depth: 2, self_ns: 2985 * MS }),
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("trace view", () => {
  it("opens on the span that held the request up and explains it", async () => {
    mockApi([[/^\/api\/traces\//, () => json(TRACE)]]);
    renderAt(`/traces/${TRACE_ID}`, <TraceDetail />, "/traces/:traceId");

    const summary = await screen.findByRole("region", { name: "Where the time went" });
    expect(summary).toHaveTextContent("payments POST /charges");
    expect(summary).toHaveTextContent("kept the caller waiting");
    const details = screen.getByRole("complementary", { name: "Selected span" });
    expect(within(details).getByRole("heading", { name: "POST /charges" })).toBeInTheDocument();
    // It ran on after orders gave up, and the screen says so.
    expect(details).toHaveTextContent("Kept running after orders stopped waiting");
  });

  it("loads a span's logs when its bar is clicked", async () => {
    const calls = mockApi([
      [/^\/api\/traces\//, () => json(TRACE)],
      [/^\/api\/logs$/, () => json({ logs: [{ time: "2026-10-22T08:04:44.867Z", service: "orders", severity: "ERROR",
        message: "order 7c1e failed: payments timed out after 2.0s, releasing stock", trace_id: TRACE_ID, span_id: "od", attributes: {} }], next_cursor: null })],
    ]);
    renderAt(`/traces/${TRACE_ID}`, <TraceDetail />, "/traces/:traceId");

    await userEvent.click(await screen.findByRole("button", { name: /orders POST \/orders.*error/ }));
    expect(await screen.findByText(/payments timed out after 2.0s/)).toBeInTheDocument();
    expect(calls.some((c) => c.includes(`trace_id=${TRACE_ID}`) && c.includes("span_id=od"))).toBe(true);
    expect(screen.getByRole("button", { name: /orders POST \/orders/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("says a missing trace may still be arriving", async () => {
    mockApi([]);
    renderAt(`/traces/${TRACE_ID}`, <TraceDetail />, "/traces/:traceId");
    expect(await screen.findByText("Trace not found")).toBeInTheDocument();
    expect(screen.getByText(/may still be on its way/)).toBeInTheDocument();
  });
});

describe("overview", () => {
  it("shows the server's explanation and retries on request", async () => {
    let fail = true;
    const services = { from: "", to: "", services: [{ service: "orders", health: "healthy", last_seen: new Date().toISOString(),
      requests: 40, errors: 0, error_rate: 0, requests_per_second: 0.04, latency_ms: { p50: null, p95: null, p99: null } }] };
    mockApi([
      [/^\/api\/services$/, () => (fail ? json({ title: "Time window too large", status: 400, detail: "at most 30 days" }, 400) : json(services))],
      [/^\/api\/dependencies$/, () => json({ edges: [] })],
      [/^\/api\/traces$/, () => json({ traces: [], next_cursor: null })],
      [/^\/api\/services\/.*\/metrics$/, () => json({ service: "orders", step: "1m", points: [] })],
    ]);
    renderAt("/", <Overview />, "/");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Time window too large");
    fail = false;
    await userEvent.click(within(alert).getByRole("button", { name: /try again/i }));
    await waitFor(() => expect(screen.getByText("No service needs attention")).toBeInTheDocument());
  });

  it("guides a first-time user when nothing has reported yet", async () => {
    mockApi([
      [/^\/api\/services$/, () => json({ from: "", to: "", services: [] })],
      [/^\/api\/dependencies$/, () => json({ edges: [] })],
      [/^\/api\/traces$/, () => json({ traces: [], next_cursor: null })],
    ]);
    renderAt("/", <Overview />, "/");
    expect(await screen.findByText("No telemetry yet")).toBeInTheDocument();
    expect(screen.getByText(/localhost:8080\/checkout/)).toBeInTheDocument();
  });

  it("never calls silence healthy", async () => {
    const quiet = { service: "inventory", health: "no_data", last_seen: null, requests: 0, errors: 0, error_rate: null,
      requests_per_second: 0, latency_ms: { p50: null, p95: null, p99: null } };
    mockApi([
      [/^\/api\/services$/, () => json({ from: "", to: "", services: [quiet] })],
      [/^\/api\/dependencies$/, () => json({ edges: [] })],
      [/^\/api\/traces$/, () => json({ traces: [], next_cursor: null })],
      [/^\/api\/services\/.*\/metrics$/, () => json({ service: "inventory", step: "1m", points: [] })],
    ]);
    renderAt("/", <Overview />, "/");
    expect(await screen.findByText(/inventory sent nothing in the last 2 minutes/)).toBeInTheDocument();
    expect(screen.getAllByText("no data").length).toBeGreaterThan(0);
  });
});

describe("service map", () => {
  it("names every call for screen readers, marking the failing ones", () => {
    // Regression: with a failing call, the label used to list ONLY the failures.
    render(
      <ServiceMap
        health={{ gateway: "degraded", orders: "healthy", payments: "healthy" }}
        edges={[
          { caller: "gateway", callee: "orders", calls: 100, errors: 25, error_rate: 0.25 },
          { caller: "orders", callee: "payments", calls: 120, errors: 18, error_rate: 0.15 },
        ]}
      />,
    );
    const map = screen.getByRole("img", { name: /^Service map/ });
    expect(map).toHaveAccessibleName(
      "Service map, 1 failing: gateway calls orders (failing, 25% errors), orders calls payments.",
    );
  });
});
