import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Config, Incident, IncidentDetail as Detail, ScenariosResponse } from "../api/types";
import { AdminProvider } from "../components/Admin";
import { RuleForm } from "../components/RuleForm";
import { formatCountdown, formatSpan, formatValue } from "../lib/alerts";
import { Alerts } from "./Alerts";
import { IncidentDetail } from "./IncidentDetail";
import { Scenarios } from "./Scenarios";

const TOKEN = "the-right-admin-token-1234";

interface Call {
  method: string;
  path: string;
  auth: string | null;
  body: unknown;
}
type Answer = (url: URL, call: Call) => Response;

function mockApi(routes: [string, RegExp, Answer][]): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://test");
    const headers = new Headers(init.headers);
    const call: Call = {
      method: init.method ?? "GET",
      path: url.pathname + url.search,
      auth: headers.get("Authorization"),
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    for (const [method, pattern, answer] of routes) {
      if (method === call.method && pattern.test(url.pathname)) return answer(url, call);
    }
    return json({ title: "Not found", status: 404 }, 404);
  });
  return calls;
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const authorized = (call: Call) => call.auth === `Bearer ${TOKEN}`;
const unauthorized = () => json({ title: "Admin token required", status: 401, detail: "send Authorization: Bearer <admin token>" }, 401);

function renderAt(path: string, element: React.ReactNode, routePath: string, token: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AdminProvider initialToken={token}>
          <Routes>
            <Route path={routePath} element={element} />
          </Routes>
        </AdminProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const MINUTE = 60_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function incident(over: Partial<Incident> = {}): Incident {
  return {
    id: 7, rule_id: 1, rule_name: "Checkout errors", kind: "error_rate", service: "gateway", threshold: 0.1,
    state: "open", opened_at: ago(3 * MINUTE), acknowledged_at: null, resolved_at: null, peak_value: 0.574,
    sample_trace_ids: ["9445ba770d815b99802a1abf56a9f473"], ...over,
  };
}

const RULES = { rules: [] };
const NONE = { incidents: [], next_cursor: null };

afterEach(() => vi.unstubAllGlobals());

describe("alerts page", () => {
  it("groups one outage's alerts by service and shows each one's numbers", async () => {
    const open = [incident(), incident({ id: 8, rule_name: "Checkout latency (p95)", kind: "p95_latency", threshold: 1000, peak_value: 2394.9 })];
    mockApi([
      ["GET", /^\/api\/incidents$/, (u) => json(u.searchParams.get("state") === "open" ? { incidents: open, next_cursor: null } : NONE)],
      ["GET", /^\/api\/alert-rules$/, () => json(RULES)],
    ]);
    renderAt("/alerts", <Alerts />, "/alerts");

    const group = await screen.findByRole("region", { name: "2 alerts on gateway" });
    expect(group).toHaveTextContent("likely one problem, several symptoms");
    const errors = within(group).getByRole("article", { name: "Checkout errors on gateway" });
    expect(errors).toHaveTextContent("57%");
    expect(errors).toHaveTextContent("limit 10%");
    expect(within(group).getByRole("article", { name: "Checkout latency (p95) on gateway" })).toHaveTextContent("2.39 s");
    expect(within(errors).getByRole("link", { name: "Investigate" })).toHaveAttribute("href", "/alerts/incidents/7");
  });

  it("asks for the admin token, refuses a wrong one, and acknowledges with the right one", async () => {
    const calls = mockApi([
      ["GET", /^\/api\/incidents$/, (u) => json(u.searchParams.get("state") === "open" ? { incidents: [incident()], next_cursor: null } : NONE)],
      ["GET", /^\/api\/alert-rules$/, () => json(RULES)],
      ["GET", /^\/api\/audit$/, (_u, c) => (authorized(c) ? json({ entries: [] }) : unauthorized())],
      ["POST", /^\/api\/incidents\/7\/ack$/, (_u, c) => (authorized(c) ? json({ ...incident(), acknowledged_at: ago(0), events: [], rule: null }) : unauthorized())],
    ]);
    renderAt("/alerts", <Alerts />, "/alerts");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /Acknowledge/ }));
    const dialog = screen.getByRole("dialog", { name: "Unlock admin" });
    await user.type(within(dialog).getByPlaceholderText("Admin token"), "a-wrong-token");
    await user.click(within(dialog).getByRole("button", { name: /Unlock/ }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Admin token required");
    expect(calls.some((c) => c.path.startsWith("/api/incidents/7/ack"))).toBe(false); // nothing was attempted

    await user.clear(within(dialog).getByPlaceholderText("Admin token"));
    await user.type(within(dialog).getByPlaceholderText("Admin token"), TOKEN);
    await user.click(within(dialog).getByRole("button", { name: /Unlock/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/incidents/7/ack")).toBe(true));
    const ack = calls.find((c) => c.method === "POST");
    expect(ack?.auth).toBe(`Bearer ${TOKEN}`);
    // The token lives in memory only: never written to storage or a cookie.
    expect(JSON.stringify({ ...localStorage })).not.toContain(TOKEN);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(TOKEN);
    expect(document.cookie).not.toContain(TOKEN);
  });
});

describe("incident page", () => {
  it("tells the story in order, with the traces to open", async () => {
    const detail: Detail = {
      ...incident({ state: "resolved", resolved_at: ago(30_000) }),
      rule: null,
      events: [
        { time: ago(3 * MINUTE), type: "opened", value: 0.574, detail: "error rate above 10% over 2 min, for 30 s", sample_trace_ids: ["9445ba770d815b99802a1abf56a9f473"] },
        { time: ago(2 * MINUTE), type: "acknowledged", value: null, detail: "by admin@127.0.0.1", sample_trace_ids: [] },
        { time: ago(30_000), type: "resolved", value: null, detail: "back within the threshold for 60 s", sample_trace_ids: [] },
      ],
    };
    mockApi([
      ["GET", /^\/api\/incidents\/7$/, () => json(detail)],
      ["GET", /^\/api\/services\/gateway\/metrics$/, () => json({ service: "gateway", step: "1m", points: [] })],
    ]);
    renderAt("/alerts/incidents/7", <IncidentDetail />, "/alerts/incidents/:incidentId");

    const timeline = await screen.findByRole("region", { name: "Timeline" });
    const items = within(timeline).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".tl-label")?.textContent)).toEqual(["Alert fired", "Acknowledged", "Resolved"]);
    expect(items[0]).toHaveTextContent("57%");
    expect(within(items[0] as HTMLElement).getByRole("link", { name: "9445ba77" })).toHaveAttribute("href", "/traces/9445ba770d815b99802a1abf56a9f473");
    expect(screen.getByRole("link", { name: /Open a failing trace/ })).toHaveAttribute("href", "/traces/9445ba770d815b99802a1abf56a9f473");
    expect(screen.getByText("resolved")).toBeInTheDocument();
  });

  it("says when an incident no longer exists", async () => {
    mockApi([]);
    renderAt("/alerts/incidents/99", <IncidentDetail />, "/alerts/incidents/:incidentId");
    expect(await screen.findByText("Incident not found")).toBeInTheDocument();
  });
});

const CONFIG: Config = {
  env: "demo", admin_enabled: true, scenarios_enabled: true,
  alerts: { enabled: true, interval_s: 15, settle_s: 30 },
  health: { degraded_error_rate: 0.05, degraded_p95_ms: 1000, down_error_rate: 0.5, no_data_after_s: 120 },
  latency: { bucket_edges_ms: [5, 10], min_samples: 20 },
  ingest: { max_body_bytes: 4194304, write_slots: 8, max_attributes_bytes: 8192 },
  retention_days: { spans: 7, logs: 7, rollups: 30, incidents: 90 },
};

const SCENARIOS: ScenariosResponse = {
  scenarios: [{ name: "payments_timeout", title: "Payments timeout", target: "payments", fault: { delay_ms: 3000 }, what_happens: "Card charges take 3 s.", what_to_look_for: "Checkout errors fires." }],
  max_duration_s: 600,
  active: null,
  targets: { payments: "reachable", orders: "reachable" },
};

describe("demo scenarios", () => {
  it("explains the locks and offers nothing to start when injection is off", async () => {
    const calls = mockApi([["GET", /^\/api\/config$/, () => json({ ...CONFIG, scenarios_enabled: false })]]);
    renderAt("/scenarios", <Scenarios />, "/scenarios");

    const locks = await screen.findByRole("list", { name: "Safety locks" });
    expect(within(locks).getAllByRole("listitem").map((li) => li.className)).toEqual(["open", "closed", "closed"]);
    expect(screen.getByText(/Failure injection is switched off on this server/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Start/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/scenarios"))).toBe(false);
  });

  it("starts a scenario for the chosen time and shows it running", async () => {
    const calls = mockApi([
      ["GET", /^\/api\/config$/, () => json(CONFIG)],
      ["GET", /^\/api\/scenarios$/, () => json(SCENARIOS)],
      ["POST", /^\/api\/scenarios\/payments_timeout\/start$/, (_u, c) =>
        authorized(c) ? json({ ...SCENARIOS, active: { name: "payments_timeout", target: "payments", remaining_s: 120 } }) : unauthorized()],
    ]);
    renderAt("/scenarios", <Scenarios />, "/scenarios", TOKEN);
    const user = userEvent.setup();

    const card = await screen.findByRole("article", { name: "Payments timeout" });
    await user.selectOptions(within(card).getByRole("combobox"), "2");
    await user.click(within(card).getByRole("button", { name: "Start Payments timeout" }));

    const running = await screen.findByRole("region", { name: "Running scenario" });
    expect(running).toHaveTextContent(/Payments timeout is running · [12]:\d\d left/);
    const start = calls.find((c) => c.method === "POST");
    expect(start?.body).toEqual({ duration_s: 120 });
    expect(start?.auth).toBe(`Bearer ${TOKEN}`);
  });
});

describe("rule form", () => {
  it("sends an error rate as a share and shows the server's reason when refused", async () => {
    const saved: unknown[] = [];
    const onSave = vi.fn(async (input: unknown) => {
      saved.push(input);
      const { ApiError } = await import("../api/client");
      throw new ApiError(422, "Invalid request", "check: threshold");
    });
    render(<RuleForm services={["gateway", "orders"]} onSave={onSave} onCancel={() => undefined} />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "Orders errors");
    await user.selectOptions(screen.getByLabelText("Service"), "orders");
    await user.clear(screen.getByLabelText("Above (%)"));
    await user.type(screen.getByLabelText("Above (%)"), "25");
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid request: check: threshold");
    expect(saved[0]).toMatchObject({ name: "Orders errors", kind: "error_rate", service: "orders", threshold: 0.25 });
  });

  it("has no threshold for 'unavailable': the window is the condition", async () => {
    render(<RuleForm services={[]} onSave={async () => undefined} onCancel={() => undefined} />);
    await userEvent.selectOptions(screen.getByLabelText("Measure"), "unavailable");
    expect(screen.queryByLabelText(/^Above/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Silent for")).toBeInTheDocument();
  });
});

describe("alert formatting", () => {
  it("formats each kind in its own unit", () => {
    expect(formatValue("error_rate", 0.574)).toBe("57%");
    expect(formatValue("p95_latency", 2394.9)).toBe("2.39 s");
    expect(formatValue("unavailable", 0)).toBe("silent");
    expect(formatValue("failure_rise", 52.8)).toBe("52.8×");
    expect(formatValue("error_rate", null)).toBe("–");
  });

  it("formats durations and countdowns", () => {
    expect(formatSpan(45_000)).toBe("45 s");
    expect(formatSpan(162_000)).toBe("2 min");
    expect(formatSpan(73 * 60_000)).toBe("1 h 13 min");
    expect(formatCountdown(125.7)).toBe("2:05");
    expect(formatCountdown(-3)).toBe("0:00");
  });
});
