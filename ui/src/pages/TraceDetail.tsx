import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { SpanView, TraceDetail as Trace } from "../api/types";
import { HttpStatus } from "../components/Bits";
import { ArrowLeft, CopyIcon, ErrorIcon } from "../components/Icons";
import { Empty, ErrorState, Skeleton } from "../components/States";
import { formatClock, formatNs } from "../lib/format";
import { layoutBars, serviceColor, ticks, whereTimeWent } from "../lib/waterfall";

function nsToIso(startNs: string, offsetNs = 0): string {
  // BigInt keeps the nanoseconds exact; only the final millisecond value becomes a Number.
  const ms = Number((BigInt(startNs) + BigInt(Math.round(offsetNs))) / 1_000_000n);
  return new Date(ms).toISOString();
}

function Summary({ trace }: { trace: Trace }) {
  const summary = whereTimeWent(trace);
  const hot = summary.hotspot;
  if (!hot || summary.rootDurationNs === 0) return null;
  const pct = Math.round(hot.shareOfRoot * 100);
  return (
    <section className="panel" aria-label="Where the time went" style={{ display: "flex", flexWrap: "wrap", gap: 20, padding: "16px 20px", alignItems: "center" }}>
      <div style={{ flex: "1 1 420px", minWidth: 0 }}>
        <div className="chart-title" style={{ textTransform: "uppercase", letterSpacing: ".06em", fontWeight: 600 }}>Where the time went</div>
        <div style={{ fontSize: 16, lineHeight: 1.45 }}>
          <strong style={{ color: hot.span.status === "error" || pct >= 50 ? "var(--bad)" : "var(--text)" }}>
            {hot.span.service} {hot.span.operation}
          </strong>{" "}
          kept the caller waiting for <span className="mono">{formatNs(hot.heldNs)}</span>
          {pct >= 1 ? <>, <span className="mono">{pct}%</span> of the {formatNs(summary.rootDurationNs)} it waited.</> : ", a small part of the total."}
          {hot.span.self_ns > hot.heldNs * 1.05 && (
            <> It kept working for <span className="mono">{formatNs(hot.span.self_ns - hot.heldNs)}</span> after the caller had its answer.</>
          )}
          {hot.span.error_type && <> Failure reason: <span className="mono">{hot.span.error_type}</span>.</>}
        </div>
      </div>
      <div style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <div className="share-bar" role="img" aria-label={summary.byService.map((s) => `${s.service} ${Math.round(s.share * 100)}%`).join(", ")}>
          {summary.byService.map((s) => (
            <div key={s.service} className="grow-x" style={{ width: `${s.share * 100}%`, background: serviceColor(s.service) }} />
          ))}
        </div>
        <div className="chips">
          {summary.byService.map((s) => (
            <span key={s.service} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--muted)" }}>
              <span className="wf-dot" style={{ background: serviceColor(s.service) }} /> {s.service} {Math.round(s.share * 100)}%
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function Waterfall({ trace, selected, onSelect }: { trace: Trace; selected: string; onSelect: (id: string) => void }) {
  const bars = layoutBars(trace);
  const marks = ticks(trace.duration_ns);
  const root = trace.spans.find((s) => s.depth === 0);
  const answerPct = root && root.duration_ns < trace.duration_ns ? ((root.offset_ns + root.duration_ns) / trace.duration_ns) * 100 : null;

  return (
    <section className="panel" aria-label="Span waterfall" style={{ overflow: "hidden" }}>
      <div className="table-wrap">
        <div className="waterfall">
          <div className="wf-head">
            <div className="wf-name-col chart-title" style={{ paddingBottom: 10, margin: 0, textTransform: "uppercase", letterSpacing: ".06em", fontWeight: 600, fontSize: 11 }}>
              Service and operation
            </div>
            <div className="wf-track" style={{ height: 40 }} aria-hidden="true">
              {marks.map((m) => (
                <span key={m.ns} className="wf-tick mono" style={{ left: `${m.pct}%` }}>{m.ns === 0 ? "0" : formatNs(m.ns)}</span>
              ))}
            </div>
          </div>
          {bars.map((b) => {
            const s = b.span;
            const cls = ["wf-bar", "grow-x", s.on_critical_path ? "crit" : "", s.status === "error" ? "err" : "", b.outlivesParent ? "after" : ""].join(" ");
            return (
              <button
                key={s.span_id}
                type="button"
                className="wf-row"
                aria-pressed={s.span_id === selected}
                onClick={() => onSelect(s.span_id)}
                aria-label={`${s.service} ${s.operation}, ${formatNs(s.duration_ns)}${s.status === "error" ? ", error" : ""}${s.log_count ? `, ${s.log_count} log lines` : ""}`}
              >
                <span className="wf-name-col">
                  <span style={{ flex: "none", width: s.depth * 14 }} />
                  <span className="wf-dot" style={{ background: serviceColor(s.service) }} />
                  <span className="wf-service">{s.service}</span>
                  <span className="wf-op">{s.operation}</span>
                  {s.status === "error" && <span style={{ color: "var(--bad)", display: "inline-flex" }}><ErrorIcon size={13} /></span>}
                  {s.log_count > 0 && <span className="chip mono" style={{ marginLeft: "auto" }}>{s.log_count}</span>}
                </span>
                <span className="wf-track row">
                  {answerPct !== null && <span className="wf-answer" style={{ left: `${answerPct}%` }} />}
                  <span className={cls} style={{ left: `${b.leftPct}%`, width: `${b.widthPct}%` }} />
                  <span
                    className={`wf-label mono ${b.labelSide}`}
                    style={b.labelSide === "before" ? { right: `${100 - b.labelLeftPct}%` } : { left: `${b.labelLeftPct}%` }}
                  >
                    {formatNs(s.duration_ns)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="legend">
        <span><span className="swatch" style={{ background: "var(--bar-crit)", boxShadow: "0 0 0 1.5px var(--accent)" }} />Critical path: what the request waited on</span>
        <span><span className="swatch" style={{ background: "var(--bar-err)" }} />Error</span>
        <span><span className="swatch" style={{ background: "var(--bar)", backgroundImage: "repeating-linear-gradient(135deg, transparent 0 5px, rgba(255,255,255,.18) 5px 7px)" }} />Still running after its caller finished</span>
        {answerPct !== null && <span><span style={{ width: 0, height: 12, borderLeft: "1px dashed var(--bad-line)" }} />The caller got its answer here</span>}
      </div>
    </section>
  );
}

function SpanDetails({ trace, span }: { trace: Trace; span: SpanView }) {
  const logs = useQuery({
    queryKey: ["logs", trace.trace_id, span.span_id],
    queryFn: ({ signal }) => api.logs({ trace_id: trace.trace_id, span_id: span.span_id, limit: 100 }, signal),
    enabled: span.log_count > 0,
  });
  const attrs = Object.entries(span.attributes).sort(([a], [b]) => a.localeCompare(b));
  const parent = trace.spans.find((s) => s.span_id === span.parent_span_id);
  const outlived = parent && span.offset_ns + span.duration_ns > parent.offset_ns + parent.duration_ns;

  return (
    <aside className="narrow" aria-label="Selected span" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="wf-dot" style={{ background: serviceColor(span.service), width: 9, height: 9 }} />
          <span className="muted" style={{ fontSize: 13 }}>{span.service} · {span.kind}</span>
        </div>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600, overflowWrap: "anywhere" }}>{span.operation}</h2>
        <div className="stat-grid">
          <div className="stat"><div className="k">Duration</div><div className="v mono">{formatNs(span.duration_ns)}</div></div>
          <div className="stat"><div className="k">Own time</div><div className="v mono">{formatNs(span.self_ns)}</div></div>
          <div className="stat">
            <div className="k">Status</div>
            <div className="v mono" style={{ color: span.status === "error" ? "var(--bad)" : undefined }}>{span.http_status ?? span.status}</div>
          </div>
        </div>
        {(span.status === "error" || outlived || span.starts_before_parent) && (
          <div className="callout">
            {span.error_type && <div>Failure reason: <span className="mono">{span.error_type}</span></div>}
            {span.status_message && <div>{span.status_message}</div>}
            {outlived && <div>Kept running after {parent?.service} stopped waiting for it. Any work it did then, the caller never heard about.</div>}
            {span.starts_before_parent && <div>Starts before its parent: the two services' clocks disagree.</div>}
            {span.status === "error" && !span.error_type && !span.status_message && <div>Marked as an error by {span.service}.</div>}
          </div>
        )}
        <div>
          <div className="kv"><span className="k mono">span_id</span><span className="v mono">{span.span_id}</span></div>
          <div className="kv"><span className="k mono">started</span><span className="v mono">+{formatNs(span.offset_ns)} · {formatClock(nsToIso(trace.start_ns, span.offset_ns), true)}</span></div>
          {attrs.map(([k, v]) => (
            <div key={k} className="kv"><span className="k mono">{k}</span><span className="v mono">{typeof v === "string" ? v : JSON.stringify(v)}</span></div>
          ))}
          {span.attributes_truncated && <div className="muted" style={{ fontSize: 12, paddingTop: 6 }}>Some large attributes were dropped on the way in.</div>}
        </div>
      </div>

      <div className="panel" id="logs" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>Logs from this span</h2>
          <Link to={`/logs?trace_id=${trace.trace_id}`} style={{ fontSize: 13 }}>All logs for this trace</Link>
        </div>
        {span.log_count === 0 ? (
          <div className="muted" style={{ fontSize: 13 }}>This span wrote no log lines. Spans with a number next to them did.</div>
        ) : logs.isPending ? (
          <Skeleton rows={2} />
        ) : logs.isError ? (
          <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
        ) : (
          logs.data.logs.map((line, i) => (
            <div key={`${line.time}-${i}`} className={`log ${line.severity}`}>
              <div className="meta"><span className="mono">{formatClock(line.time, true)}</span><span className={`sev mono ${line.severity}`}>{line.severity}</span></div>
              <div className="msg mono">{line.message}</div>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}

export function TraceDetail() {
  const { traceId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [copied, setCopied] = useState(false);
  const trace = useQuery({
    queryKey: ["trace", traceId],
    queryFn: ({ signal }) => api.trace(traceId, signal),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });

  const data = trace.data;
  const root = data?.spans.find((s) => s.depth === 0) ?? data?.spans[0];
  const hot = data ? whereTimeWent(data).hotspot?.span.span_id : undefined;
  const selectedId = params.get("span") ?? hot ?? root?.span_id ?? "";
  const selected = data?.spans.find((s) => s.span_id === selectedId) ?? root;
  const select = (id: string) =>
    setParams((p) => {
      const next = new URLSearchParams(p);
      next.set("span", id);
      return next;
    }, { replace: true });

  return (
    <>
      <nav aria-label="Breadcrumb" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, fontSize: 13 }} className="muted">
        <Link to={`/traces${params.get("range") ? `?range=${params.get("range")}` : ""}`} style={{ display: "inline-flex", alignItems: "center", gap: 6, minHeight: 32 }}>
          <ArrowLeft size={14} /> Traces
        </Link>
        <span aria-hidden="true">/</span>
        <span className="mono" style={{ color: "var(--text)", overflowWrap: "anywhere" }}>{traceId}</span>
      </nav>

      {trace.isPending ? (
        <div className="panel"><Skeleton rows={8} /></div>
      ) : trace.isError ? (
        <div className="panel">
          {trace.error instanceof ApiError && trace.error.status === 404 ? (
            <Empty title="Trace not found">It may still be on its way (spans arrive within a few seconds), or it is older than the 7-day retention.</Empty>
          ) : (
            <ErrorState error={trace.error} onRetry={() => void trace.refetch()} />
          )}
        </div>
      ) : data && root && selected ? (
        <>
          <header className="page-head">
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
                <h1>{root.service} {root.operation}</h1>
                <HttpStatus code={root.http_status} errors={data.error_count} />
              </div>
              <div className="sub" style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
                <span>Started <span className="mono" style={{ color: "var(--text)" }}>{formatClock(nsToIso(data.start_ns), true)}</span></span>
                <span>Caller waited <span className="mono" style={{ color: "var(--text)" }}>{formatNs(root.duration_ns)}</span></span>
                {root.duration_ns < data.duration_ns && <span>Whole trace <span className="mono" style={{ color: "var(--text)" }}>{formatNs(data.duration_ns)}</span></span>}
                <span>
                  <span className="mono" style={{ color: "var(--text)" }}>{data.span_count}</span> spans ·{" "}
                  <span className="mono" style={{ color: "var(--text)" }}>{data.services.length}</span> services ·{" "}
                  <span className="mono" style={{ color: data.error_count ? "var(--bad)" : "var(--text)" }}>{data.error_count}</span> errors
                </span>
              </div>
            </div>
            <div className="toolbar">
              <button
                type="button"
                className="btn"
                onClick={() => {
                  void navigator.clipboard?.writeText(data.trace_id).then(() => setCopied(true));
                }}
              >
                <CopyIcon /> {copied ? "Copied" : "Copy trace ID"}
              </button>
            </div>
          </header>
          <Summary trace={data} />
          <div className="split">
            <div className="wide"><Waterfall trace={data} selected={selected.span_id} onSelect={select} /></div>
            <SpanDetails trace={data} span={selected} />
          </div>
        </>
      ) : null}
    </>
  );
}
