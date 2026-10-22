import type { EventType, Incident, RuleKind, RuleStateName } from "../api/types";
import { formatMs, formatRate } from "./format";

/** What each rule kind measures, in words, and how its threshold is typed in. */
export const KINDS: Record<
  RuleKind,
  { label: string; help: string; unit: string; toInput: (t: number) => number; fromInput: (v: number) => number }
> = {
  error_rate: {
    label: "Error rate",
    help: "Share of requests that failed. Not judged below 10 requests in the window.",
    unit: "%",
    toInput: (t) => Math.round(t * 1000) / 10,
    fromInput: (v) => v / 100,
  },
  p95_latency: {
    label: "p95 latency",
    help: "95% of requests were faster than this. Needs at least 20 requests.",
    unit: "ms",
    toInput: (t) => t,
    fromInput: (v) => v,
  },
  unavailable: {
    label: "Unavailable",
    help: "No requests at all for the whole window. The window is the condition; there is no threshold.",
    unit: "",
    toInput: () => 0,
    fromInput: () => 0,
  },
  failure_rise: {
    label: "Failures rising",
    help: "Errors per minute in the window, as a multiple of the 30 minutes before it.",
    unit: "×",
    toInput: (t) => t,
    fromInput: (v) => v,
  },
};

/** A measured value or threshold in the rule's own unit. */
export function formatValue(kind: RuleKind, value: number | null): string {
  if (value === null) return "–";
  switch (kind) {
    case "error_rate":
      return formatRate(value);
    case "p95_latency":
      return formatMs(value);
    case "unavailable":
      return value === 0 ? "silent" : `${Math.round(value)} requests`;
    case "failure_rise":
      return `${value.toFixed(1)}×`;
  }
}

export const STATE_TEXT: Record<RuleStateName, string> = {
  ok: "ok",
  pending: "pending",
  firing: "firing",
  recovering: "recovering",
  no_data: "no data",
};

export const EVENT_TEXT: Record<EventType, string> = {
  opened: "Alert fired",
  peak: "New peak",
  breached_again: "Breached again",
  no_data: "Data stopped",
  data_back: "Data back",
  acknowledged: "Acknowledged",
  resolved: "Resolved",
};

/** 45 s, 3 min, 1 h 12 min. */
export function formatSpan(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function incidentDuration(i: Incident, now: number = Date.now()): string {
  const end = i.resolved_at ? new Date(i.resolved_at).getTime() : now;
  return formatSpan(end - new Date(i.opened_at).getTime());
}

/** "4:05" from seconds, for scenario countdowns. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
