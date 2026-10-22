import type { SpanView, TraceDetail } from "../api/types";

export interface Bar {
  span: SpanView;
  leftPct: number;
  widthPct: number;
  /** Where the duration label goes: after the bar, inside it, or before it near the edge. */
  labelLeftPct: number;
  labelSide: "after" | "inside" | "before";
  /** True when this span is still running after its parent finished. */
  outlivesParent: boolean;
}

const MIN_WIDTH_PCT = 0.35; // a 1 µs span must still be visible and clickable

export function layoutBars(trace: TraceDetail): Bar[] {
  const total = Math.max(trace.duration_ns, 1);
  const byId = new Map(trace.spans.map((s) => [s.span_id, s]));
  return trace.spans.map((span) => {
    const leftPct = (span.offset_ns / total) * 100;
    const widthPct = Math.max((span.duration_ns / total) * 100, MIN_WIDTH_PCT);
    const end = leftPct + widthPct;
    // Room after the bar? Then after. A wide bar reaching the edge? Inside it. A short bar
    // at the very end of the trace? Before it, or the label would run off the right edge.
    const labelSide = end <= 86 ? "after" : widthPct >= 14 ? "inside" : "before";
    const parent = span.parent_span_id ? byId.get(span.parent_span_id) : undefined;
    const outlivesParent =
      parent !== undefined && span.offset_ns + span.duration_ns > parent.offset_ns + parent.duration_ns;
    const labelLeftPct = labelSide === "after" ? end : leftPct;
    return { span, leftPct, widthPct, labelLeftPct, labelSide, outlivesParent };
  });
}

/** "Nice" tick marks: 0, 500 ms, 1 s, 1.5 s ... at most `maxTicks` of them. */
export function ticks(durationNs: number, maxTicks = 7): { ns: number; pct: number }[] {
  const durationMs = Math.max(durationNs / 1e6, 0.001);
  // The smallest round step (1, 2, 2.5 or 5 times a power of ten) that fits in maxTicks.
  const magnitude = 10 ** Math.floor(Math.log10(durationMs / maxTicks));
  const candidates = [1, 2, 2.5, 5, 10, 20].map((m) => m * magnitude);
  const step =
    candidates.find((s) => Math.floor(durationMs / s) + 1 <= maxTicks) ?? magnitude * 50;
  const out = [];
  for (let ms = 0; ms <= durationMs + 1e-9; ms += step) {
    out.push({ ns: ms * 1e6, pct: (ms / durationMs) * 100 });
  }
  return out;
}

export interface Hotspot {
  span: SpanView;
  heldNs: number; // own time while the caller was still waiting
  shareOfRoot: number; // heldNs as a share of what the caller (root span) waited
}

export interface ServiceShare {
  service: string;
  ns: number;
  share: number;
}

export interface TimeSummary {
  rootDurationNs: number;
  hotspot: Hotspot | null;
  byService: ServiceShare[];
}

/**
 * Where the time went: own time per service, counted only while the root was still waiting
 * (work after the caller got its answer didn't make anyone wait), plus the single span with
 * the most own time.
 */
export function whereTimeWent(trace: TraceDetail): TimeSummary {
  const root = trace.spans.find((s) => s.depth === 0) ?? trace.spans[0];
  if (!root) return { rootDurationNs: 0, hotspot: null, byService: [] };
  const rootEnd = root.offset_ns + root.duration_ns;
  const counted = (s: SpanView) => {
    // Own time is spread over the span; count the part before the root finished.
    const end = s.offset_ns + s.duration_ns;
    if (s.duration_ns === 0) return 0;
    const inside = Math.max(0, Math.min(end, rootEnd) - s.offset_ns) / s.duration_ns;
    return s.self_ns * inside;
  };
  const perService = new Map<string, number>();
  let best: SpanView | null = null;
  let bestNs = -1;
  for (const span of trace.spans) {
    const ns = counted(span);
    perService.set(span.service, (perService.get(span.service) ?? 0) + ns);
    if (ns > bestNs) {
      bestNs = ns;
      best = span;
    }
  }
  const sum = [...perService.values()].reduce((a, b) => a + b, 0) || 1;
  const byService = [...perService.entries()]
    .map(([service, ns]) => ({ service, ns, share: ns / sum }))
    .sort((a, b) => b.ns - a.ns);
  return {
    rootDurationNs: root.duration_ns,
    hotspot: best
      ? { span: best, heldNs: bestNs, shareOfRoot: root.duration_ns ? bestNs / root.duration_ns : 0 }
      : null,
    byService,
  };
}

const PALETTE = ["#7c9cff", "#c792ea", "#46c28e", "#f0b429", "#5cc8ff", "#ff9e64", "#e38fd0", "#9ccc65"];

/** A stable colour per service name, so a service looks the same on every screen. */
export function serviceColor(service: string): string {
  let hash = 0;
  for (const ch of service) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const known: Record<string, string> = {
    gateway: "#7c9cff",
    orders: "#c792ea",
    inventory: "#46c28e",
    payments: "#f0b429",
  };
  return known[service] ?? PALETTE[hash % PALETTE.length] ?? "#7c9cff";
}
