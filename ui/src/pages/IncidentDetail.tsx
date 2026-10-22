import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { admin, api, ApiError } from "../api/client";
import type { IncidentDetail as Detail, IncidentEvent, MetricPoint, RuleKind } from "../api/types";
import { AdminButton, useAdmin } from "../components/Admin";
import { LineChart, type SeriesPoint } from "../components/Charts";
import { ArrowLeft, ArrowRight } from "../components/Icons";
import { Empty, ErrorState, Skeleton } from "../components/States";
import { EVENT_TEXT, formatValue, incidentDuration } from "../lib/alerts";
import { formatClock, formatMs, formatNumber, formatRate, shortId } from "../lib/format";

const BEFORE_MS = 15 * 60_000; // context before the alert fired
const AFTER_MS = 5 * 60_000;

/** The value the rule watches, read off one metric point. */
function metricOf(kind: RuleKind, p: MetricPoint): number | null {
  switch (kind) {
    case "error_rate":
      return p.error_rate;
    case "p95_latency":
      return p.latency_ms?.p95?.ms ?? null;
    case "unavailable":
      return p.requests ?? 0; // silence is the point here: draw it as zero, not as a gap
    case "failure_rise":
      return p.errors ?? 0;
  }
}

const CHART: Record<RuleKind, { title: string; format: (v: number) => string; limit: boolean }> = {
  error_rate: { title: "Error rate per minute", format: formatRate, limit: true },
  p95_latency: { title: "p95 latency per minute", format: formatMs, limit: true },
  unavailable: { title: "Requests per minute", format: formatNumber, limit: false },
  failure_rise: { title: "Errors per minute", format: formatNumber, limit: false },
};

function Chart({ incident }: { incident: Detail }) {
  const metrics = useQuery({
    queryKey: ["metrics", incident.service, "incident", incident.id, incident.state],
    // The window is worked out at fetch time, so each refresh of an open incident reaches "now".
    queryFn: ({ signal }) => {
      const now = Date.now();
      const opened = new Date(incident.opened_at).getTime();
      const end = Math.min(incident.resolved_at ? new Date(incident.resolved_at).getTime() + AFTER_MS : now, now);
      const step = end - opened > 20 * 3600_000 ? "5m" : "1m";
      const window = { from: new Date(opened - BEFORE_MS).toISOString(), to: new Date(end).toISOString() };
      return api.metrics(incident.service, window, step, signal);
    },
    refetchInterval: incident.state === "open" ? 15_000 : false,
  });
  const spec = CHART[incident.kind];
  if (metrics.isPending) return <Skeleton rows={4} />;
  if (metrics.isError) return <ErrorState error={metrics.error} onRetry={() => void metrics.refetch()} />;
  const points: SeriesPoint[] = metrics.data.points.map((p) => ({ t: p.t, v: metricOf(incident.kind, p) }));
  return (
    <LineChart
      title={`${incident.service}: ${spec.title}`}
      points={points}
      color="var(--accent)"
      format={spec.format}
      limit={spec.limit ? incident.threshold : undefined}
      limitLabel={spec.limit ? `alert above ${formatValue(incident.kind, incident.threshold)}` : undefined}
      band={{ from: incident.opened_at, to: incident.resolved_at, label: incident.state === "open" ? "open" : "incident" }}
    />
  );
}

function TimelineEvent({ event, kind }: { event: IncidentEvent; kind: RuleKind }) {
  return (
    <li className={`tl-item ${event.type}`}>
      <span className="tl-dot" aria-hidden="true" />
      <div className="tl-head">
        <span className="tl-label">{EVENT_TEXT[event.type]}</span>
        <span className="mono muted">{formatClock(event.time)}</span>
        {event.value !== null && <span className="mono tl-value">{formatValue(kind, event.value)}</span>}
      </div>
      {event.detail && <div className="tl-detail">{event.detail}</div>}
      {event.sample_trace_ids.length > 0 && (
        <div className="chips" style={{ marginTop: 6 }}>
          {event.sample_trace_ids.map((id) => (
            <Link key={id} to={`/traces/${id}`} className="chip mono trace-chip">{shortId(id)}</Link>
          ))}
        </div>
      )}
    </li>
  );
}

export function IncidentDetail() {
  const { incidentId } = useParams();
  const id = Number(incidentId);
  const { run } = useAdmin();
  const client = useQueryClient();
  const incident = useQuery({
    queryKey: ["incident", id],
    queryFn: ({ signal }) => api.incident(id, signal),
    enabled: Number.isInteger(id) && id > 0,
    refetchInterval: (q) => (q.state.data?.state === "resolved" ? false : 5_000),
  });
  const ack = useMutation({
    mutationFn: () => run((t) => admin.acknowledge(t, id)),
    onSuccess: (data) => {
      client.setQueryData(["incident", id], data);
      void client.invalidateQueries({ queryKey: ["incidents"] });
    },
  });

  const back = (
    <Link to="/alerts" className="muted" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 }}>
      <ArrowLeft size={14} /> Alerts
    </Link>
  );
  if (!Number.isInteger(id) || id <= 0) {
    return <>{back}<div className="panel"><Empty title="Not an incident number" /></div></>;
  }
  if (incident.isPending) return <>{back}<div className="panel"><Skeleton rows={6} /></div></>;
  if (incident.isError) {
    const missing = incident.error instanceof ApiError && incident.error.status === 404;
    return (
      <>
        {back}
        <div className="panel">
          {missing ? (
            <Empty title="Incident not found">Resolved incidents are kept for 90 days.</Empty>
          ) : (
            <ErrorState error={incident.error} onRetry={() => void incident.refetch()} />
          )}
        </div>
      </>
    );
  }

  const d = incident.data;
  const samples = d.events.find((e) => e.type === "opened")?.sample_trace_ids ?? d.sample_trace_ids;
  const open = d.state === "open";
  return (
    <>
      {back}
      <header className="page-head">
        <div>
          <h1 style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            {d.rule_name} <span className="chip" style={{ fontSize: 13 }}>{d.service}</span>
            <span className={`pill ${open ? "down" : "healthy"}`}>{open ? "open" : "resolved"}</span>
            {d.acknowledged_at && open && <span className="status">acknowledged</span>}
          </h1>
          <div className="sub">
            #{d.id} · {d.rule?.description ?? "rule since deleted"} · {open ? "open for" : "lasted"} {incidentDuration(d)}
          </div>
        </div>
        <div className="toolbar">
          {open && !d.acknowledged_at && (
            <AdminButton onClick={() => ack.mutate()} disabled={ack.isPending}>Acknowledge</AdminButton>
          )}
          {samples[0] && (
            <Link className="btn btn-danger" to={`/traces/${samples[0]}`}>
              Open a failing trace <ArrowRight size={14} />
            </Link>
          )}
        </div>
      </header>
      {ack.isError && <div className="form-error" role="alert">{ack.error.message}</div>}

      <section className="grid-4" aria-label="Incident numbers">
        <div className="panel kpi">
          <span className="label">Peak</span>
          <span className="value mono bad-text">{formatValue(d.kind, d.peak_value)}</span>
          <span className="note">{d.kind === "unavailable" ? "no requests at all" : `limit ${formatValue(d.kind, d.threshold)}`}</span>
        </div>
        <div className="panel kpi">
          <span className="label">Fired</span>
          <span className="value mono">{formatClock(d.opened_at)}</span>
          <span className="note">{new Date(d.opened_at).toLocaleDateString("en-GB")}</span>
        </div>
        <div className="panel kpi">
          <span className="label">{open ? "Open for" : "Resolved"}</span>
          <span className="value mono">{open ? incidentDuration(d) : formatClock(d.resolved_at ?? d.opened_at)}</span>
          <span className="note">{open ? "until the rule is clear for its recovery time" : `after ${incidentDuration(d)}`}</span>
        </div>
        <div className="panel kpi">
          <span className="label">Linked traces</span>
          <span className="value mono">{samples.length}</span>
          <span className="note">captured when it fired</span>
        </div>
      </section>

      <div className="split">
        <section className="panel wide" aria-label="Metric around the incident">
          <div className="panel-head">
            <h2>What the rule saw</h2>
            <span className="hint">15 minutes before it fired, until it resolved</span>
          </div>
          <div className="panel-body"><Chart incident={d} /></div>
          {samples.length > 0 && (
            <div className="panel-body">
              <div className="chart-title">Evidence: requests that failed or were slowest when it fired</div>
              <div className="evidence">
                {samples.map((t) => (
                  <Link key={t} to={`/traces/${t}`} className="evidence-row">
                    <span className="mono">{t}</span>
                    <span className="muted">open trace <ArrowRight size={12} /></span>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </section>

        <section className="panel narrow" aria-label="Timeline">
          <div className="panel-head">
            <h2>Timeline</h2>
            {open && <span className="hint right">updates live</span>}
          </div>
          <ol className="timeline">
            {d.events.map((e, i) => <TimelineEvent key={`${e.time}-${i}`} event={e} kind={d.kind} />)}
            {open && (
              <li className="tl-item waiting">
                <span className="tl-dot" aria-hidden="true" />
                <div className="tl-head"><span className="tl-label muted">Still open</span></div>
                <div className="tl-detail">
                  Resolves by itself once the rule has been clear for {d.rule?.recover_s ?? "its recovery"} s.
                </div>
              </li>
            )}
          </ol>
        </section>
      </div>
    </>
  );
}
