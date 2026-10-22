import type { Edge } from "../api/types";

export interface Node {
  service: string;
  layer: number;
  x: number;
  y: number;
}

export interface Layout {
  nodes: Node[];
  width: number;
  height: number;
}

export const NODE_W = 120;
export const NODE_H = 46;
const GAP_X = 20;
const GAP_Y = 54;

/**
 * Lays services out top to bottom by call depth: entry points (called by nobody) in the
 * first row, each callee one row below its deepest caller. Top to bottom fits the narrow
 * panel the map lives in. Cycles can't push a node further than the number of services,
 * so the loop always ends.
 */
export function layoutGraph(services: string[], edges: Edge[]): Layout {
  const names = [...new Set([...services, ...edges.flatMap((e) => [e.caller, e.callee])])].sort();
  const layer = new Map(names.map((n) => [n, 0]));
  for (let pass = 0; pass < names.length; pass++) {
    let changed = false;
    for (const e of edges) {
      const want = (layer.get(e.caller) ?? 0) + 1;
      if (want > (layer.get(e.callee) ?? 0) && want < names.length) {
        layer.set(e.callee, want);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const columns = new Map<number, string[]>();
  for (const n of names) {
    const l = layer.get(n) ?? 0;
    columns.set(l, [...(columns.get(l) ?? []), n]);
  }
  const widest = Math.max(1, ...[...columns.values()].map((c) => c.length));
  const width = widest * NODE_W + (widest - 1) * GAP_X;
  const nodes: Node[] = [];
  for (const [l, row] of columns) {
    const rowWidth = row.length * NODE_W + (row.length - 1) * GAP_X;
    row.forEach((service, i) => {
      nodes.push({ service, layer: l, x: (width - rowWidth) / 2 + i * (NODE_W + GAP_X), y: l * (NODE_H + GAP_Y) });
    });
  }
  const layers = Math.max(1, columns.size);
  return { nodes, width, height: layers * NODE_H + (layers - 1) * GAP_Y };
}
