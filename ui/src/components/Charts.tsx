import { formatClock } from "../lib/format";

/** A tiny trend line. Missing points break the line instead of dropping to zero. */
export function Sparkline({
  values,
  color,
  height = 26,
  max,
}: {
  values: (number | null)[];
  color: string;
  height?: number;
  max?: number;
}) {
  const known = values.filter((v): v is number => v !== null);
  const top = max ?? Math.max(...known, 0) * 1.1;
  const step = values.length > 1 ? 120 / (values.length - 1) : 0;
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((v, i) => {
    if (v === null) {
      if (current.length) segments.push(current.join(" "));
      current = [];
      return;
    }
    const y = height - 2 - (top > 0 ? (v / top) * (height - 4) : 0);
    current.push(`${(i * step).toFixed(1)},${y.toFixed(1)}`);
  });
  if (current.length) segments.push(current.join(" "));
  return (
    <svg viewBox={`0 0 120 ${height}`} preserveAspectRatio="none" style={{ width: "100%", height, display: "block" }} aria-hidden="true">
      <line x1={0} x2={120} y1={height - 2} y2={height - 2} stroke="var(--line)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      {segments.map((points) =>
        points.includes(" ") ? (
          <polyline key={points} points={points} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        ) : (
          <circle key={points} cx={points.split(",")[0]} cy={points.split(",")[1]} r={1.5} fill={color} />
        ),
      )}
    </svg>
  );
}

export interface SeriesPoint {
  t: string;
  v: number | null;
}

/** A labelled time-series chart with a y axis, gaps for missing data, an optional limit
 * line and an optional shaded period (an incident, from when it fired to when it resolved). */
export function LineChart({
  title,
  points,
  color,
  format,
  limit,
  limitLabel,
  band,
}: {
  title: string;
  points: SeriesPoint[];
  color: string;
  format: (v: number) => string;
  limit?: number;
  limitLabel?: string;
  band?: { from: string; to: string | null; label: string };
}) {
  // Close to the width it is shown at, so 11 px labels stay 11 px.
  const W = 440;
  const H = 170;
  const PAD = { l: 58, r: 12, t: 12, b: 24 };
  const known = points.map((p) => p.v).filter((v): v is number => v !== null);
  const top = Math.max(...known, limit ?? 0, 0) * 1.15 || 1;
  const x = (i: number) => PAD.l + (points.length > 1 ? (i / (points.length - 1)) * (W - PAD.l - PAD.r) : 0);
  const y = (v: number) => PAD.t + (1 - v / top) * (H - PAD.t - PAD.b);
  const paths: string[] = [];
  const dots: { cx: number; cy: number }[] = [];
  let d = "";
  let run = 0;
  const close = () => {
    if (run > 1) paths.push(d);
    d = "";
    run = 0;
  };
  points.forEach((p, i) => {
    if (p.v === null) return close();
    d += `${d ? "L" : "M"}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`;
    run += 1;
    // A value with gaps on both sides has no line to sit on: draw it as a dot.
    const prev = points[i - 1]?.v ?? null;
    const next = points[i + 1]?.v ?? null;
    if (prev === null && next === null) dots.push({ cx: x(i), cy: y(p.v) });
  });
  close();
  // Points are evenly spaced in time, so a timestamp maps linearly between the first and last.
  const first = points.length ? new Date(points[0]?.t ?? 0).getTime() : 0;
  const last = points.length ? new Date(points[points.length - 1]?.t ?? 0).getTime() : 0;
  const xAt = (iso: string) => {
    const span = last - first || 1;
    const f = Math.min(1, Math.max(0, (new Date(iso).getTime() - first) / span));
    return PAD.l + f * (W - PAD.l - PAD.r);
  };
  const yTicks = [0, top / 2, top];
  const xLabels = points.length > 1 ? [0, Math.floor((points.length - 1) / 2), points.length - 1] : [0];
  const summary = known.length ? `latest ${format(known[known.length - 1] ?? 0)}, highest ${format(Math.max(...known))}` : "no data in this window";

  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="chart-title">{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} role="img" aria-label={`${title}: ${summary}`}>
        {band && points.length > 1 && (
          <g>
            <rect
              x={xAt(band.from)}
              y={PAD.t}
              width={Math.max(2, (band.to ? xAt(band.to) : W - PAD.r) - xAt(band.from))}
              height={H - PAD.t - PAD.b}
              fill="var(--bad)"
              opacity={0.1}
            />
            <line x1={xAt(band.from)} x2={xAt(band.from)} y1={PAD.t} y2={H - PAD.b} stroke="var(--bad)" strokeDasharray="3 3" />
            {/* Label on the side with room: right of the line, or left when it starts near the end. */}
            <text
              x={xAt(band.from) > W - PAD.r - 60 ? xAt(band.from) - 4 : xAt(band.from) + 4}
              textAnchor={xAt(band.from) > W - PAD.r - 60 ? "end" : "start"}
              y={PAD.t + 11}
              fontSize="11"
              fill="var(--bad)"
            >
              {band.label}
            </text>
          </g>
        )}
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--line-soft)" />
            <text x={PAD.l - 8} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)" className="mono">{format(v)}</text>
          </g>
        ))}
        {xLabels.map((i) => (
          <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"} fontSize="11" fill="var(--muted)" className="mono">
            {points[i] ? formatClock(points[i].t).slice(0, 5) : ""}
          </text>
        ))}
        {limit !== undefined && (
          <g>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(limit)} y2={y(limit)} stroke="var(--warn)" strokeDasharray="4 4" />
            {limitLabel && <text x={W - PAD.r} y={y(limit) - 5} textAnchor="end" fontSize="11" fill="var(--warn)">{limitLabel}</text>}
          </g>
        )}
        {paths.map((p) => (
          <path key={p} d={p} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />
        ))}
        {dots.map((dot) => (
          <circle key={`${dot.cx}-${dot.cy}`} cx={dot.cx} cy={dot.cy} r={3.5} fill={color} />
        ))}
        {!known.length && (
          <text x={W / 2} y={H / 2} textAnchor="middle" fontSize="13" fill="var(--muted)">No requests in this window</text>
        )}
      </svg>
    </figure>
  );
}
