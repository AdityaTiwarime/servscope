import { ApiError } from "../api/client";
import { formatClock } from "../lib/format";
import { AlertIcon, ErrorIcon, RetryIcon } from "./Icons";

export function Skeleton({ rows = 4, height = 18 }: { rows?: number; height?: number }) {
  return (
    <div role="status" aria-label="Loading" style={{ display: "flex", flexDirection: "column", gap: 10, padding: "8px 18px 18px" }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height, width: `${92 - i * 9}%` }} />
      ))}
    </div>
  );
}

function describe(error: unknown): { title: string; detail: string } {
  if (error instanceof ApiError) return { title: error.title, detail: error.detail };
  return { title: "Something went wrong", detail: error instanceof Error ? error.message : "" };
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, detail } = describe(error);
  return (
    <div className="state error" role="alert">
      <ErrorIcon size={22} />
      <div className="state-title">{title}</div>
      {detail && <div>{detail}</div>}
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry} style={{ marginTop: 6 }}>
          <RetryIcon /> Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="state">
      <div className="state-title">{title}</div>
      {children && <div style={{ maxWidth: 460 }}>{children}</div>}
    </div>
  );
}

/** Shown when a refresh failed but older data is still on screen: never pass it off as live. */
export function StaleBanner({ error, updatedAt }: { error: unknown; updatedAt: number }) {
  const { title } = describe(error);
  return (
    <div className="banner stale" role="status">
      <span className="icon"><AlertIcon size={18} /></span>
      <div className="text">
        <div className="title">Showing data from {formatClock(new Date(updatedAt).toISOString())}</div>
        <div className="detail">Live updates stopped: {title}. Retrying every 10 seconds.</div>
      </div>
    </div>
  );
}
