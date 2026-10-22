import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Edge, Health, ServiceSummary } from "../api/types";
import { HealthPill, HttpStatus } from "../components/Bits";
import { Sparkline } from "../components/Charts";
import { AlertIcon, ArrowRight, CheckIcon } from "../components/Icons";
import { RangePicker, TraceSearch, useRange } from "../components/Layout";
import { ServiceMap } from "../components/ServiceMap";
import { IncidentGroups, useOpenIncidents } from "./Alerts";
import { Empty, ErrorState, Skeleton, StaleBanner } from "../components/States";
import { formatAgo, formatClock, formatMs, formatNumber, formatPercentile, formatRate, shortId } from "../lib/format";
import { STEP, windowFor, type Range } from "../lib/time";

const REFRESH_MS = 10_000;

/** Services nobody calls are where customers come in: their numbers are what users feel. */
export function frontDoor(services: ServiceSummary[], edges: Edge[]): ServiceSummary[] {
  const called = new Set(edges.map((e) => e.callee));
  const entry = services.filter((s) => !called.has(s.service) && s.requests > 0);
  return entry.length ? entry : services;
}

export function attention(services: ServiceSummary[]): ServiceSummary[] {
  const order: Record<Health, number> = { down: 0, degraded: 1, no_data: 2, healthy: 3 };
  return services.filter((s) => s.health === "down" || s.health === "degraded").sort((a, b) => order[a.health] - order[b.health]);
}

function describeProblem(s: ServiceSummary): string {
  const parts = [];
  if ((s.error_rate ?? 0) >= 0.05) parts.push(`${formatRate(s.error_rate)} of requests failing`);
  if (s.latency_ms.p95 && s.latency_ms.p95.ms >= 1000) parts.push(`p95 latency ${formatPercentile(s.latency_ms.p95)}`);
  return parts.join(", ") || "slower or less reliable than usual";
}

function useOverview(range: Range) {
  const services = useQuery({
    queryKey: ["services", range],
    queryFn: ({ signal }) => api.services(windowFor(range), signal),
    refetchInterval: REFRESH_MS,
  });
  const deps = useQuery({
    queryKey: ["dependencies", range],
    queryFn: ({ signal }) => api.dependencies(windowFor(range), signal),
    refetchInterval: REFRESH_MS,
  });
  const failing = useQuery({
    queryKey: ["traces", "errors", range],
    queryFn: ({ signal }) => api.traces({ ...windowFor(range), status: "error", limit: 8 }, signal),
    refetchInterval: REFRESH_MS,
  });
  const names = services.data?.services.map((s) => s.service) ?? [];
  const series = useQueries({
    queries: names.map((name) => ({
      queryKey: ["metrics", name, range],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.metrics(name, windowFor(range), STEP[range], signal),
      refetchInterval: REFRESH_MS,
    })),
  });
  const errorSeries: Record<string, (number | null)[]> = {};
  names.forEach((name, i) => {
    errorSeries[name] = series[i]?.data?.points.map((p) => p.error_rate) ?? [];
  });
  return { services, deps, failing, errorSeries };
}

export function Overview() {
  const [range] = useRange();
  const { services, deps, failing, errorSeries } = useOverview(range);
  const incidents = useOpenIncidents().data?.incidents ?? [];
  const list = services.data?.services ?? [];
  const edges = deps.data?.edges ?? [];
  const door = frontDoor(list, edges);
  const requests = door.reduce((n, s) => n + s.requests, 0);
  const errors = door.reduce((n, s) => n + s.errors, 0);
  const rps = door.reduce((n, s) => n + s.requests_per_second, 0);
  const worstP95 = door.map((s) => s.latency_ms.p95).filter((p) => p !== null).sort((a, b) => b.ms - a.ms)[0] ?? null;
  const problems = attention(list);
  const health = Object.fromEntries(list.map((s) => [s.service, s.health]));
  const maxTrace = Math.max(...(failing.data?.traces.map((t) => t.duration_ms) ?? [0]), 1);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Overview</h1>
          <div className="sub">
            {list.length ? `${list.length} services · front door: ${door.map((s) => s.service).join(", ")}` : "Waiting for telemetry"}
          </div>
        </div>
        <div className="toolbar">
          <TraceSearch />
          <RangePicker />
          <span className="live">
            <span className={`live-dot ${services.isError ? "stale" : "pulse"}`} />
            {services.isError ? "Updates paused" : `Live · every ${REFRESH_MS / 1000} s`}
          </span>
        </div>
      </header>

      {services.isError && services.data && <StaleBanner error={services.error} updatedAt={services.dataUpdatedAt} />}

      {incidents.length > 0 && (
        <section className="incident-list" aria-label="Active alerts">
          <IncidentGroups incidents={incidents} />
        </section>
      )}

      {services.isPending ? (
        <div className="panel"><Skeleton rows={2} /></div>
      ) : services.isError && !services.data ? (
        <div className="panel"><ErrorState error={services.error} onRetry={() => void services.refetch()} /></div>
      ) : !list.length ? (
        <div className="panel">
          <Empty title="No telemetry yet">
            Nothing has reported to ServScope. Start the demo stack and place an order:{" "}
            <code className="mono">curl -X POST localhost:8080/checkout -H "content-type: application/json" -d '{`{"items":[{"sku":"tea","qty":1}]}`}'</code>
          </Empty>
        </div>
      ) : (
        <>
          {problems.length ? (
            <section className="banner fade-in" aria-label="Needs attention">
              <span className="icon"><AlertIcon size={18} /></span>
              <div className="text">
                <div className="title">
                  {problems[0]?.service} is {problems[0]?.health}: {problems[0] && describeProblem(problems[0])}
                </div>
                <div className="detail">
                  {problems.length > 1 ? `Also affected: ${problems.slice(1).map((s) => `${s.service} (${s.health})`).join(", ")}. ` : ""}
                  Health covers the last 5 minutes.
                </div>
              </div>
              <Link className="btn btn-danger" to={`/traces?service=${problems[0]?.service ?? ""}&status=error&range=${range}`}>
                See failing traces <ArrowRight size={14} />
              </Link>
            </section>
          ) : (
            <section className="banner ok fade-in" aria-label="Status">
              <span className="icon"><CheckIcon size={18} /></span>
              <div className="text">
                <div className="title">No service needs attention</div>
                <div className="detail">
                  {list.filter((s) => s.health === "no_data").length
                    ? `${list.filter((s) => s.health === "no_data").map((s) => s.service).join(", ")} sent nothing in the last 2 minutes, so its health is unknown.`
                    : "Every service answered within limits over the last 5 minutes."}
                </div>
              </div>
            </section>
          )}

          <section className="grid-4" aria-label={`Key numbers, last ${range}`}>
            <div className="panel kpi">
              <span className="label">Requests per second</span>
              <span className="value mono">{formatNumber(rps)}</span>
              <span className="note">{requests.toLocaleString("en-US")} requests in {range}</span>
            </div>
            <div className="panel kpi">
              <span className="label">Error rate</span>
              <span className="value mono" style={{ color: errors / (requests || 1) >= 0.05 ? "var(--bad)" : undefined }}>
                {requests ? formatRate(errors / requests) : "–"}
              </span>
              <span className="note">{errors.toLocaleString("en-US")} failed at the front door</span>
            </div>
            <div className="panel kpi">
              <span className="label">p95 latency</span>
              <span className="value mono" style={{ color: worstP95 && worstP95.ms >= 1000 ? "var(--warn)" : undefined }}>
                {formatPercentile(worstP95)}
              </span>
              <span className="note">{worstP95 ? "95% of requests were faster" : "needs at least 20 requests"}</span>
            </div>
            <div className="panel kpi">
              <span className="label">Services needing attention</span>
              <span className="value mono" style={{ color: problems.length ? "var(--bad)" : undefined }}>{problems.length}</span>
              <span className="note">of {list.length} reporting</span>
            </div>
          </section>

          <div className="split">
            <section className="panel wide" aria-label="Services">
              <div className="panel-head">
                <h2>Services</h2>
                <span className="hint">Numbers for the last {range}; health for the last 5 minutes</span>
              </div>
              <div className="table-wrap">
                <table className="data" style={{ minWidth: 700 }}>
                  <thead>
                    <tr>
                      <th scope="col">Service</th>
                      <th scope="col">Health</th>
                      <th scope="col" className="num">Req/s</th>
                      <th scope="col" className="num">Errors</th>
                      <th scope="col" className="num">p50</th>
                      <th scope="col" className="num">p95</th>
                      <th scope="col" className="num">p99</th>
                      <th scope="col">Error rate over time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((s) => (
                      <tr key={s.service}>
                        <td>
                          <Link className="row-link" to={`/services?service=${s.service}&range=${range}`}>{s.service}</Link>
                          <div className="muted" style={{ fontSize: 11 }}>last seen {formatAgo(s.last_seen)}</div>
                        </td>
                        <td><HealthPill health={s.health} /></td>
                        <td className="num mono">{formatNumber(s.requests_per_second)}</td>
                        <td className="num mono" style={{ color: (s.error_rate ?? 0) >= 0.05 ? "var(--bad)" : undefined }}>{formatRate(s.error_rate)}</td>
                        <td className="num mono muted">{formatPercentile(s.latency_ms.p50)}</td>
                        <td className="num mono">{formatPercentile(s.latency_ms.p95)}</td>
                        <td className="num mono muted">{formatPercentile(s.latency_ms.p99)}</td>
                        <td style={{ width: 160 }}>
                          <Sparkline values={errorSeries[s.service] ?? []} max={1} color={s.health === "down" ? "var(--bad)" : s.health === "degraded" ? "var(--warn)" : "var(--ok)"} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="panel narrow" aria-label="Service map">
              <div className="panel-head">
                <h2>Service map</h2>
                <span className="hint right">From real calls</span>
              </div>
              <div className="panel-body">
                {deps.isError && !deps.data ? (
                  <ErrorState error={deps.error} onRetry={() => void deps.refetch()} />
                ) : (
                  <ServiceMap edges={edges} health={health} />
                )}
              </div>
            </section>
          </div>

          <section className="panel" aria-label="Failing traces">
            <div className="panel-head">
              <h2>Failing requests</h2>
              <span className="hint">Newest first</span>
              <Link className="right" to={`/traces?status=error&range=${range}`}>All failing traces</Link>
            </div>
            {failing.isPending ? (
              <Skeleton rows={3} />
            ) : failing.isError && !failing.data ? (
              <ErrorState error={failing.error} onRetry={() => void failing.refetch()} />
            ) : !failing.data?.traces.length ? (
              <Empty title={`No failed requests in the last ${range}`}>Every trace in this window finished without an error.</Empty>
            ) : (
              <div className="table-wrap">
                <table className="data" style={{ minWidth: 720 }}>
                  <thead>
                    <tr>
                      <th scope="col">Time</th>
                      <th scope="col">Request</th>
                      <th scope="col">Status</th>
                      <th scope="col" className="num">Duration</th>
                      <th scope="col" style={{ width: "30%" }}>Compared with the others</th>
                      <th scope="col">Services</th>
                    </tr>
                  </thead>
                  <tbody>
                    {failing.data.traces.map((t) => (
                      <tr key={t.trace_id}>
                        <td className="mono muted">{formatClock(t.start_time)}</td>
                        <td>
                          <Link className="row-link" to={`/traces/${t.trace_id}?range=${range}`}>{t.root_operation}</Link>{" "}
                          <span className="mono muted" style={{ fontSize: 12 }}>{shortId(t.trace_id)}</span>
                        </td>
                        <td><HttpStatus code={t.http_status} errors={t.error_count} /></td>
                        <td className="num mono">{formatMs(t.duration_ms)}</td>
                        <td>
                          <div className="share-bar" style={{ height: 8 }}>
                            <div className="grow-x" style={{ width: `${(t.duration_ms / maxTrace) * 100}%`, background: "var(--bad-strong)" }} />
                          </div>
                        </td>
                        <td><span className="chips">{t.services.map((s) => <span key={s} className="chip">{s}</span>)}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}
