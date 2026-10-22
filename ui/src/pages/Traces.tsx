import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { HttpStatus } from "../components/Bits";
import { RangePicker, TraceSearch, useRange } from "../components/Layout";
import { Empty, ErrorState, Skeleton, StaleBanner } from "../components/States";
import { formatClock, formatMs, shortId } from "../lib/format";
import { windowFor } from "../lib/time";

export function Traces() {
  const [range] = useRange();
  const [params, setParams] = useSearchParams();
  const service = params.get("service") ?? "";
  const status = params.get("status") ?? "";
  const minMs = params.get("min_ms") ?? "";
  const operation = params.get("operation") ?? "";
  // Cursors of the pages before this one, so "Newer" can go back without page numbers.
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1] ?? null;

  const setFilter = (key: string, value: string) => {
    setCursors([]);
    setParams((p) => {
      const next = new URLSearchParams(p);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  };

  const services = useQuery({
    queryKey: ["services", range],
    queryFn: ({ signal }) => api.services(windowFor(range), signal),
  });
  const traces = useQuery({
    queryKey: ["traces", range, service, status, minMs, operation, cursor],
    queryFn: ({ signal }) =>
      api.traces(
        { ...windowFor(range), service, status, operation, min_duration_ms: minMs || undefined, limit: 25, cursor },
        signal,
      ),
    placeholderData: keepPreviousData,
    refetchInterval: cursor ? false : 10_000, // only the newest page follows live data
  });
  const list = traces.data?.traces ?? [];
  const maxMs = Math.max(...list.map((t) => t.duration_ms), 1);
  const filtered = Boolean(service || status || minMs || operation);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Traces</h1>
          <div className="sub">Requests with at least one span matching the filters, newest first</div>
        </div>
        <div className="toolbar">
          <TraceSearch />
          <RangePicker />
        </div>
      </header>

      <form className="panel" style={{ display: "flex", flexWrap: "wrap", gap: 12, padding: 14, alignItems: "center" }} onSubmit={(e) => e.preventDefault()} aria-label="Filter traces">
        <label className="field">
          <span>Service</span>
          <select value={service} onChange={(e) => setFilter("service", e.target.value)}>
            <option value="">Any</option>
            {services.data?.services.map((s) => <option key={s.service} value={s.service}>{s.service}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Status</span>
          <select value={status} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">Any</option>
            <option value="error">Has an error</option>
            <option value="ok">No errors</option>
          </select>
        </label>
        <label className="field">
          <span>Slower than</span>
          <select value={minMs} onChange={(e) => setFilter("min_ms", e.target.value)}>
            <option value="">Any duration</option>
            <option value="100">100 ms</option>
            <option value="500">500 ms</option>
            <option value="1000">1 s</option>
            <option value="2000">2 s</option>
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 220px" }}>
          <span>Operation</span>
          <input
            defaultValue={operation}
            placeholder="e.g. POST /checkout"
            onBlur={(e) => setFilter("operation", e.target.value.trim())}
            onKeyDown={(e) => {
              if (e.key === "Enter") setFilter("operation", e.currentTarget.value.trim());
            }}
            style={{ width: "100%" }}
          />
        </label>
        {filtered && (
          <button type="button" className="btn" onClick={() => { setCursors([]); setParams(range ? { range } : {}); }}>
            Clear filters
          </button>
        )}
      </form>

      {traces.isError && traces.data && <StaleBanner error={traces.error} updatedAt={traces.dataUpdatedAt} />}

      <section className="panel" aria-label="Trace results">
        {traces.isPending ? (
          <Skeleton rows={6} />
        ) : traces.isError && !traces.data ? (
          <ErrorState error={traces.error} onRetry={() => void traces.refetch()} />
        ) : !list.length ? (
          <Empty title={filtered ? "No traces match these filters" : `No traces in the last ${range}`}>
            {filtered ? "Try a longer time range or fewer filters." : "Place an order on the demo shop and it will appear here within a few seconds."}
          </Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 820 }}>
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Request</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="num">Duration</th>
                    <th scope="col" style={{ width: "26%" }}>Duration on this page</th>
                    <th scope="col" className="num">Spans</th>
                    <th scope="col">Services</th>
                  </tr>
                </thead>
                <tbody style={{ opacity: traces.isPlaceholderData ? 0.6 : 1 }}>
                  {list.map((t) => (
                    <tr key={t.trace_id}>
                      <td className="mono muted">{formatClock(t.start_time)}</td>
                      <td>
                        <Link className="row-link" to={`/traces/${t.trace_id}?range=${range}`} data-trace-id={t.trace_id}>
                          {t.root_service} {t.root_operation}
                        </Link>{" "}
                        <span className="mono muted" style={{ fontSize: 12 }}>{shortId(t.trace_id)}</span>
                      </td>
                      <td><HttpStatus code={t.http_status} errors={t.error_count} /></td>
                      <td className="num mono">{formatMs(t.duration_ms)}</td>
                      <td>
                        <div className="share-bar" style={{ height: 8 }}>
                          <div style={{ width: `${(t.duration_ms / maxMs) * 100}%`, background: t.error_count ? "var(--bad-strong)" : "var(--bar-crit)" }} />
                        </div>
                      </td>
                      <td className="num mono">
                        {t.span_count}
                        {t.error_count > 0 && <span style={{ color: "var(--bad)" }}> · {t.error_count} err</span>}
                      </td>
                      <td><span className="chips">{t.services.map((s) => <span key={s} className="chip">{s}</span>)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", padding: 12, borderTop: "1px solid var(--line)" }}>
              <button type="button" className="btn" disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>Newer</button>
              <button type="button" className="btn" disabled={!traces.data?.next_cursor} onClick={() => traces.data?.next_cursor && setCursors([...cursors, traces.data.next_cursor])}>Older</button>
            </div>
          </>
        )}
      </section>
    </>
  );
}
