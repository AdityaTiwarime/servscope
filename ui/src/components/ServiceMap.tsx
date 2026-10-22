import type { Edge, Health } from "../api/types";
import { formatRate } from "../lib/format";
import { layoutGraph, NODE_H, NODE_W } from "../lib/graph";

const EDGE_COLOR = (rate: number) => (rate >= 0.2 ? "var(--bad-strong)" : rate > 0 ? "var(--warn)" : "var(--muted)");

/** Who calls whom, from real spans. Line colour is the error rate of that call. */
export function ServiceMap({ edges, health }: { edges: Edge[]; health: Record<string, Health> }) {
  const layout = layoutGraph(Object.keys(health), edges);
  const pos = new Map(layout.nodes.map((n) => [n.service, n]));
  const PAD = 8;
  const width = layout.width + PAD * 2;
  const height = layout.height + PAD * 2;
  const failing = edges.filter((e) => e.error_rate >= 0.2);
  const label = failing.length
    ? `Service map. Failing calls: ${failing.map((e) => `${e.caller} to ${e.callee}, ${formatRate(e.error_rate)} errors`).join("; ")}.`
    : `Service map: ${edges.map((e) => `${e.caller} calls ${e.callee}`).join(", ") || "no calls in this window"}.`;

  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", maxWidth: width * 1.15, margin: "0 auto", height: "auto", display: "block" }} role="img" aria-label={label}>
        <defs>
          <marker id="map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="var(--muted)" />
          </marker>
        </defs>
        {edges.map((e) => {
          const a = pos.get(e.caller);
          const b = pos.get(e.callee);
          if (!a || !b) return null;
          const x1 = a.x + PAD + NODE_W / 2;
          const y1 = a.y + PAD + NODE_H;
          const x2 = b.x + PAD + NODE_W / 2;
          const y2 = b.y + PAD;
          const mid = (y1 + y2) / 2;
          const color = EDGE_COLOR(e.error_rate);
          return (
            <g key={`${e.caller}-${e.callee}`}>
              <path d={`M${x1} ${y1}C${x1} ${mid} ${x2} ${mid} ${x2} ${y2 - 2}`} fill="none" stroke={color} strokeWidth={e.error_rate >= 0.2 ? 2.5 : 1.5} markerEnd="url(#map-arrow)" />
              {e.errors > 0 && (
                <text x={(x1 + x2) / 2 + 8} y={mid + 4} textAnchor="start" fontSize="11" fontWeight="600" fill={color} className="mono">
                  {formatRate(e.error_rate)} err
                </text>
              )}
            </g>
          );
        })}
        {layout.nodes.map((n) => {
          const h = health[n.service] ?? "no_data";
          const stroke = h === "down" ? "var(--bad-strong)" : h === "degraded" ? "var(--warn)" : h === "healthy" ? "var(--ok)" : "var(--line)";
          return (
            <g key={n.service} transform={`translate(${n.x + PAD} ${n.y + PAD})`}>
              <rect width={NODE_W} height={NODE_H} rx={10} fill="var(--panel-2)" stroke={stroke} strokeWidth={h === "down" ? 2 : 1.5} />
              <text x={12} y={21} fontSize="13" fontWeight="600" fill="var(--text)">{n.service}</text>
              <text x={12} y={37} fontSize="11" fill="var(--muted)">{h.replace("_", " ")}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
