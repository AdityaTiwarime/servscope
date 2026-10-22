import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { RangePicker, useRange } from "../components/Layout";
import { Empty, ErrorState, Skeleton, StaleBanner } from "../components/States";
import { formatClock, shortId } from "../lib/format";
import { windowFor } from "../lib/time";

const SEVERITIES = ["", "INFO", "WARN", "ERROR"] as const;

export function Logs() {
  const [range] = useRange();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const severity = params.get("severity") ?? "";
  const service = params.get("service") ?? "";
  const traceId = params.get("trace_id") ?? "";
  const [draft, setDraft] = useState(q);
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1] ?? null;

  const set = (key: string, value: string) => {
    setCursors([]);
    setParams((p) => {
      const next = new URLSearchParams(p);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  };

  const services = useQuery({ queryKey: ["services", range], queryFn: ({ signal }) => api.services(windowFor(range), signal) });
  const logs = useQuery({
    queryKey: ["logs", range, q, severity, service, traceId, cursor],
    queryFn: ({ signal }) =>
      api.logs(
        // One trace's logs are found by its ID however old they are, so no time window then.
        { ...(traceId ? {} : windowFor(range)), q, severity, service, trace_id: traceId, limit: 50, cursor },
        signal,
      ),
    placeholderData: keepPreviousData,
    refetchInterval: cursor ? false : 10_000,
  });
  const lines = logs.data?.logs ?? [];

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Logs</h1>
          <div className="sub">{traceId ? <>Lines from trace <span className="mono">{shortId(traceId)}</span></> : "Structured log lines from every service, newest first"}</div>
        </div>
        <div className="toolbar">{!traceId && <RangePicker />}</div>
      </header>

      <form
        className="panel"
        role="search"
        style={{ display: "flex", flexWrap: "wrap", gap: 12, padding: 14 }}
        onSubmit={(e) => {
          e.preventDefault();
          set("q", draft.trim());
        }}
      >
        <label className="field" style={{ flex: "1 1 320px" }}>
          <span className="visually-hidden">Search log text</span>
          <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Search words, e.g. timed out" style={{ width: "100%" }} maxLength={200} />
        </label>
        <label className="field">
          <span>Level</span>
          <select value={severity} onChange={(e) => set("severity", e.target.value)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s ? `${s} and above` : "Any"}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Service</span>
          <select value={service} onChange={(e) => set("service", e.target.value)}>
            <option value="">Any</option>
            {services.data?.services.map((s) => <option key={s.service} value={s.service}>{s.service}</option>)}
          </select>
        </label>
        <button type="submit" className="btn">Search</button>
        {traceId && <button type="button" className="btn" onClick={() => set("trace_id", "")}>Show all traces</button>}
      </form>

      {logs.isError && logs.data && <StaleBanner error={logs.error} updatedAt={logs.dataUpdatedAt} />}

      <section className="panel" aria-label="Log lines">
        {logs.isPending ? (
          <Skeleton rows={8} />
        ) : logs.isError && !logs.data ? (
          <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
        ) : !lines.length ? (
          <Empty title="No log lines match">Search matches whole words. Try fewer words, a lower level or a longer time range.</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 820 }}>
                <thead>
                  <tr><th scope="col">Time</th><th scope="col">Level</th><th scope="col">Service</th><th scope="col">Message</th><th scope="col">Trace</th></tr>
                </thead>
                <tbody style={{ opacity: logs.isPlaceholderData ? 0.6 : 1 }}>
                  {lines.map((l, i) => (
                    <tr key={`${l.time}-${i}`}>
                      <td className="mono muted">{formatClock(l.time, true)}</td>
                      <td><span className={`sev mono ${l.severity}`}>{l.severity}</span></td>
                      <td>{l.service}</td>
                      <td className="mono" style={{ whiteSpace: "normal", fontSize: 12, overflowWrap: "anywhere" }}>{l.message}</td>
                      <td>
                        {l.trace_id ? (
                          <Link className="mono" to={`/traces/${l.trace_id}${l.span_id ? `?span=${l.span_id}` : ""}`}>{shortId(l.trace_id)}</Link>
                        ) : <span className="muted">–</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", padding: 12, borderTop: "1px solid var(--line)" }}>
              <button type="button" className="btn" disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>Newer</button>
              <button type="button" className="btn" disabled={!logs.data?.next_cursor} onClick={() => logs.data?.next_cursor && setCursors([...cursors, logs.data.next_cursor])}>Older</button>
            </div>
          </>
        )}
      </section>
    </>
  );
}
