import type { Health } from "../api/types";

const HEALTH_TEXT: Record<Health, string> = {
  healthy: "healthy",
  degraded: "degraded",
  down: "down",
  no_data: "no data",
};

export function HealthPill({ health }: { health: Health }) {
  return <span className={`pill ${health}`}>{HEALTH_TEXT[health]}</span>;
}

export function HttpStatus({ code, errors = 0 }: { code: number | null; errors?: number }) {
  const cls = code === null ? (errors ? "err" : "") : code >= 500 ? "err" : code >= 400 ? "warn" : "ok";
  return <span className={`status mono ${cls}`}>{code ?? (errors ? "error" : "–")}</span>;
}

export function ServiceDot({ color }: { color: string }) {
  return <span className="wf-dot" style={{ background: color }} aria-hidden="true" />;
}
