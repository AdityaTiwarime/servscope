import type { Percentile } from "../api/types";

/** 0.4 ms, 42 ms, 1.23 s, 2 min 5 s: always the unit a person reads fastest. */
export function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "–";
  if (ms === 0) return "0 ms";
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 10) return `${ms.toFixed(1)} ms`;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

export const formatNs = (ns: number): string => formatMs(ns / 1e6);

export function formatPercentile(p: Percentile | null): string {
  if (p === null) return "–";
  return p.at_least ? `≥ ${formatMs(p.ms)}` : formatMs(p.ms);
}

export function formatRate(rate: number | null): string {
  if (rate === null) return "–";
  const pct = rate * 100;
  if (pct === 0) return "0%";
  if (pct < 0.1) return "<0.1%";
  return `${pct < 10 ? pct.toFixed(1) : Math.round(pct)}%`;
}

export function formatNumber(n: number): string {
  if (n >= 100) return Math.round(n).toLocaleString("en-US");
  if (n >= 10) return n.toFixed(1);
  return n.toFixed(2).replace(/\.?0+$/, "") || "0";
}

/** 10:43:56 in the viewer's time zone; with milliseconds for log lines. */
export function formatClock(iso: string, withMs = false): string {
  const d = new Date(iso);
  const hms = d.toLocaleTimeString("en-GB", { hour12: false });
  return withMs ? `${hms}.${String(d.getMilliseconds()).padStart(3, "0")}` : hms;
}

export function formatAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export const shortId = (id: string): string => id.slice(0, 8);
