import { describe, expect, it } from "vitest";

import { ApiError, apiGet, buildUrl } from "../api/client";
import type { SpanView, TraceDetail } from "../api/types";
import { formatMs, formatPercentile, formatRate } from "./format";
import { layoutGraph } from "./graph";
import { parseRange, windowFor } from "./time";
import { layoutBars, ticks, whereTimeWent } from "./waterfall";

const MS = 1_000_000;

function span(id: string, parent: string | null, offsetMs: number, durMs: number, extra: Partial<SpanView> = {}): SpanView {
  return {
    span_id: id, parent_span_id: parent, service: "orders", operation: id, kind: "server",
    offset_ns: offsetMs * MS, duration_ns: durMs * MS, self_ns: durMs * MS, depth: parent ? 1 : 0,
    status: "ok", status_message: null, http_status: null, error_type: null, attributes: {},
    attributes_truncated: false, on_critical_path: false, starts_before_parent: false, log_count: 0,
    ...extra,
  };
}

function trace(spans: SpanView[], durationMs: number): TraceDetail {
  return {
    trace_id: "t".repeat(32), start_ns: "0", duration_ns: durationMs * MS, span_count: spans.length,
    error_count: 0, services: [], critical_path: [], slowest_span_id: null, spans,
  };
}

describe("formatting", () => {
  it.each([
    [0.25, "0.25 ms"], [4.2, "4.2 ms"], [42, "42 ms"], [1234, "1.23 s"], [42_000, "42.0 s"], [125_000, "2 min 5 s"],
  ])("formatMs(%s) = %s", (ms, text) => expect(formatMs(ms)).toBe(text));

  it("marks a percentile from the open top bucket as a lower bound", () => {
    expect(formatPercentile({ ms: 10_000, at_least: true })).toBe("≥ 10.0 s");
    expect(formatPercentile(null)).toBe("–");
  });

  it.each([[0, "0%"], [0.0004, "<0.1%"], [0.079, "7.9%"], [0.31, "31%"], [null, "–"]])(
    "formatRate(%s) = %s", (rate, text) => expect(formatRate(rate)).toBe(text),
  );
});

describe("time ranges", () => {
  it("falls back to 15 minutes for anything unknown", () => {
    expect(parseRange("7d")).toBe("15m");
    expect(parseRange(null)).toBe("15m");
    expect(parseRange("6h")).toBe("6h");
  });

  it("sends ISO times with a zone, which the API requires", () => {
    const w = windowFor("1h", Date.UTC(2026, 9, 22, 10, 0, 0));
    expect(w).toEqual({ from: "2026-10-22T09:00:00.000Z", to: "2026-10-22T10:00:00.000Z" });
  });
});

describe("waterfall layout", () => {
  it("places bars by offset and keeps tiny spans visible", () => {
    const bars = layoutBars(trace([span("a", null, 0, 1000), span("b", "a", 500, 0.001)], 1000));
    expect(bars[1]?.leftPct).toBeCloseTo(50);
    expect(bars[1]?.widthPct).toBeGreaterThan(0.3);
  });

  it("keeps every duration label on screen", () => {
    const bars = layoutBars(trace([span("a", null, 0, 1000), span("b", "a", 100, 200), span("c", "a", 995, 5)], 1000));
    expect(bars.map((b) => b.labelSide)).toEqual(["inside", "after", "before"]);
  });

  it("notices a child still running after its parent finished", () => {
    const bars = layoutBars(trace([span("call", null, 0, 2000), span("pay", "call", 10, 2990)], 3000));
    expect(bars[1]?.outlivesParent).toBe(true);
  });

  it("chooses round tick steps", () => {
    expect(ticks(3021 * MS).map((t) => t.ns / MS)).toEqual([0, 500, 1000, 1500, 2000, 2500, 3000]);
    expect(ticks(42 * MS).map((t) => t.ns / MS)).toEqual([0, 10, 20, 30, 40]);
  });
});

describe("where the time went", () => {
  it("names the span with the most own time while the caller waited", () => {
    // The payments timeout: payments works 3 s, but the customer only waited 2 s of it.
    const spans = [
      span("checkout", null, 0, 2040, { self_ns: 20 * MS, service: "gateway" }),
      span("charge", "checkout", 18, 3000, { self_ns: 3000 * MS, service: "payments", depth: 1 }),
      span("reserve", "checkout", 5, 10, { self_ns: 10 * MS, service: "inventory", depth: 1 }),
    ];
    const summary = whereTimeWent(trace(spans, 3018));
    expect(summary.hotspot?.span.span_id).toBe("charge");
    // Only the 2022 ms before the customer's answer count, not all 3 s.
    expect(summary.hotspot?.shareOfRoot).toBeCloseTo(2022 / 2040, 2);
    expect(summary.hotspot?.heldNs).toBeCloseTo(2022 * MS, -3);
    expect(summary.byService[0]?.service).toBe("payments");
  });

  it("copes with an empty trace", () => {
    expect(whereTimeWent(trace([], 0)).hotspot).toBeNull();
  });
});

describe("service map layout", () => {
  it("puts callees right of their callers", () => {
    const edges = [
      { caller: "gateway", callee: "orders", calls: 1, errors: 0, error_rate: 0 },
      { caller: "orders", callee: "payments", calls: 1, errors: 0, error_rate: 0 },
      { caller: "orders", callee: "inventory", calls: 1, errors: 0, error_rate: 0 },
    ];
    const layout = layoutGraph([], edges);
    const layer = Object.fromEntries(layout.nodes.map((n) => [n.service, n.layer]));
    expect(layer).toEqual({ gateway: 0, orders: 1, payments: 2, inventory: 2 });
    // Top to bottom, and narrow enough for the side panel: two nodes side by side at most.
    expect(layout.width).toBeLessThan(300);
  });

  it("terminates on a call cycle", () => {
    const edges = [
      { caller: "a", callee: "b", calls: 1, errors: 0, error_rate: 0 },
      { caller: "b", callee: "a", calls: 1, errors: 0, error_rate: 0 },
    ];
    expect(layoutGraph([], edges).nodes).toHaveLength(2);
  });
});

describe("api client", () => {
  it("drops empty parameters", () => {
    expect(buildUrl("/api/traces", { service: "orders", status: "", cursor: null })).toBe("/api/traces?service=orders");
  });

  it("surfaces the server's problem details", async () => {
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ title: "Time window too large", status: 400, detail: "at most 1 day" }), { status: 400 });
    const error = await apiGet("/api/traces").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).title).toBe("Time window too large");
  });

  it("says the API is unreachable when the network fails", async () => {
    globalThis.fetch = async () => {
      throw new TypeError("Failed to fetch");
    };
    const error = (await apiGet("/api/services").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.title).toBe("ServScope is unreachable");
  });
});
