export const RANGES = ["15m", "1h", "6h", "24h"] as const;
export type Range = (typeof RANGES)[number];

const MINUTES: Record<Range, number> = { "15m": 15, "1h": 60, "6h": 360, "24h": 1440 };

/** The metrics step that keeps a chart between 15 and 288 points. */
export const STEP: Record<Range, "1m" | "5m" | "1h"> = { "15m": "1m", "1h": "1m", "6h": "5m", "24h": "5m" };

export function parseRange(value: string | null): Range {
  return (RANGES as readonly string[]).includes(value ?? "") ? (value as Range) : "15m";
}

/** The [from, to) window ending now, as ISO strings with a time zone (the API insists). */
export function windowFor(range: Range, now: number = Date.now()): { from: string; to: string } {
  return {
    from: new Date(now - MINUTES[range] * 60_000).toISOString(),
    to: new Date(now).toISOString(),
  };
}
