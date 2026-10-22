import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { HealthPill } from "../components/Bits";
import { LineChart } from "../components/Charts";
import { RangePicker, useRange } from "../components/Layout";
import { Empty, ErrorState, Skeleton, StaleBanner } from "../components/States";
import { formatAgo, formatMs, formatNumber, formatPercentile, formatRate } from "../lib/format";
import { STEP, windowFor } from "../lib/time";

export function Services() {
  const [range] = useRange();
  const [params, setParams] = useSearchParams();
  const services = useQuery({
    queryKey: ["services", range],
    queryFn: ({ signal }) => api.services(windowFor(range), signal),
    refetchInterval: 10_000,
  });
  const list = services.data?.services ?? [];
  const chosen = params.get("service") ?? list[0]?.service ?? "";
  const current = list.find((s) => s.service === chosen);
  const metrics = useQuery({
    queryKey: ["metrics", chosen, range],
    queryFn: ({ signal }) => api.metrics(chosen, windowFor(range), STEP[range], signal),
    enabled: Boolean(chosen),
    refetchInterval: 10_000,
  });
  const points = metrics.data?.points ?? [];

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Services</h1>
          <div className="sub">Rate, errors and latency per service, from its server spans</div>
        </div>
        <div className="toolbar"><RangePicker /></div>
      </header>

      {services.isError && services.data && <StaleBanner error={services.error} updatedAt={services.dataUpdatedAt} />}

      {services.isPending ? (
        <div className="panel"><Skeleton rows={4} /></div>
      ) : services.isError && !services.data ? (
        <div className="panel"><ErrorState error={services.error} onRetry={() => void services.refetch()} /></div>
      ) : !list.length ? (
        <div className="panel"><Empty title="No services have reported yet">Services appear here once they send their first span.</Empty></div>
      ) : (
        <div className="split">
          <div className="svc-list narrow" style={{ flex: "1 1 260px", maxWidth: 340 }} role="group" aria-label="Choose a service">
            {list.map((s) => (
              <button
                key={s.service}
                type="button"
                className="svc-item"
                aria-pressed={s.service === chosen}
                onClick={() => setParams((p) => { const n = new URLSearchParams(p); n.set("service", s.service); return n; })}
              >
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontWeight: 600 }}>{s.service}</span>
                  <span className="muted mono" style={{ fontSize: 12 }}>{formatNumber(s.requests_per_second)} req/s · {formatRate(s.error_rate)} err</span>
                </span>
                <HealthPill health={s.health} />
              </button>
            ))}
          </div>

          {current && (
            <section className="panel wide" aria-label={`${current.service} details`} style={{ padding: 20, display: "flex", flexDirection: "column", gap: 18 }}>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
                <h2 style={{ margin: 0, fontSize: 20 }}>{current.service}</h2>
                <HealthPill health={current.health} />
                <span className="muted" style={{ fontSize: 13 }}>last seen {formatAgo(current.last_seen)}</span>
                <span style={{ marginLeft: "auto", display: "flex", gap: 12, fontSize: 13 }}>
                  <Link to={`/traces?service=${current.service}&status=error&range=${range}`}>Failing traces</Link>
                  <Link to={`/logs?service=${current.service}&severity=WARN&range=${range}`}>Warnings and errors</Link>
                </span>
              </div>
              <div className="stat-grid" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
                <div className="stat"><div className="k">Requests</div><div className="v mono">{current.requests.toLocaleString("en-US")}</div></div>
                <div className="stat"><div className="k">Error rate</div><div className="v mono">{formatRate(current.error_rate)}</div></div>
                <div className="stat"><div className="k">p50</div><div className="v mono">{formatPercentile(current.latency_ms.p50)}</div></div>
                <div className="stat"><div className="k">p95</div><div className="v mono">{formatPercentile(current.latency_ms.p95)}</div></div>
                <div className="stat"><div className="k">p99</div><div className="v mono">{formatPercentile(current.latency_ms.p99)}</div></div>
              </div>
              {metrics.isPending ? (
                <Skeleton rows={3} height={60} />
              ) : metrics.isError ? (
                <ErrorState error={metrics.error} onRetry={() => void metrics.refetch()} />
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 24 }}>
                  <LineChart title="Requests per second" color="var(--accent)" format={(v) => formatNumber(v)} points={points.map((p) => ({ t: p.t, v: p.requests_per_second }))} />
                  <LineChart title="Error rate" color="var(--bad)" format={(v) => formatRate(v)} limit={0.05} limitLabel="degraded at 5%" points={points.map((p) => ({ t: p.t, v: p.error_rate }))} />
                  <LineChart title="p95 latency" color="var(--warn)" format={(v) => formatMs(v)} limit={1000} limitLabel="degraded at 1 s" points={points.map((p) => ({ t: p.t, v: p.latency_ms?.p95?.ms ?? null }))} />
                  <LineChart title="p50 latency" color="var(--ok)" format={(v) => formatMs(v)} points={points.map((p) => ({ t: p.t, v: p.latency_ms?.p50?.ms ?? null }))} />
                </div>
              )}
              <p className="muted" style={{ margin: 0, fontSize: 12 }}>
                Percentiles need at least 20 requests in a time step; steps with fewer show as gaps. Gaps also mean no requests, not zero.
              </p>
            </section>
          )}
        </div>
      )}
    </>
  );
}
