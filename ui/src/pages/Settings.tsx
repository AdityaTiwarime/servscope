import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { admin, api, ApiError } from "../api/client";
import type { Config, RetentionDays } from "../api/types";
import { AdminButton, useAdmin } from "../components/Admin";
import { LockIcon } from "../components/Icons";
import { Empty, ErrorState, Skeleton } from "../components/States";
import { formatClock, formatMs, formatRate } from "../lib/format";

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

function Retention({ days }: { days: RetentionDays }) {
  const { token, run } = useAdmin();
  const client = useQueryClient();
  const [form, setForm] = useState({ spans: days.spans, logs: days.logs, rollups: days.rollups });
  const [editing, setEditing] = useState(false);
  const save = useMutation({
    mutationFn: () => run((t) => admin.saveRetention(t, form)),
    onSuccess: () => {
      setEditing(false);
      void client.invalidateQueries({ queryKey: ["config"] });
    },
  });
  const rows: { key: keyof typeof form; label: string; max: number }[] = [
    { key: "spans", label: "Spans (raw traces)", max: 90 },
    { key: "logs", label: "Log lines", max: 90 },
    { key: "rollups", label: "Per-minute metrics", max: 365 },
  ];
  return (
    <section className="panel" aria-label="Retention">
      <div className="panel-head">
        <h2>Retention</h2>
        <span className="hint">Older data is deleted every minute, in small batches</span>
        <span className="right">
          {!editing && <AdminButton onClick={() => setEditing(true)}>Change</AdminButton>}
        </span>
      </div>
      <div className="panel-body">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          {rows.map((r) => (
            <div className="kv" key={r.key}>
              <label className="k" htmlFor={`ret-${r.key}`}>{r.label}</label>
              <span className="v">
                {editing && token ? (
                  <span className="input with-unit" style={{ width: 120, display: "inline-flex" }}>
                    <input
                      id={`ret-${r.key}`}
                      type="number"
                      min={1}
                      max={r.max}
                      required
                      value={form[r.key]}
                      onChange={(e) => setForm((f) => ({ ...f, [r.key]: Number(e.target.value) }))}
                    />
                    <span className="muted">days</span>
                  </span>
                ) : (
                  <span className="mono" id={`ret-${r.key}`}>{days[r.key]} days</span>
                )}
              </span>
            </div>
          ))}
          <Kv k="Resolved incidents and audit log" v={<span className="mono">{days.incidents} days</span>} />
          {save.isError && (
            <div className="form-error" role="alert">
              {save.error instanceof ApiError && save.error.detail ? `${save.error.title}: ${save.error.detail}` : save.error.message}
            </div>
          )}
          {editing && token && (
            <div className="dialog-actions">
              <button type="button" className="btn" onClick={() => setEditing(false)}>Cancel</button>
              <button type="submit" className="btn btn-primary" disabled={save.isPending}>Save</button>
            </div>
          )}
        </form>
      </div>
    </section>
  );
}

function Audit() {
  const { token } = useAdmin();
  const audit = useQuery({
    queryKey: ["audit", token],
    queryFn: ({ signal }) => api.audit(token ?? "", signal),
    enabled: Boolean(token),
    refetchInterval: 10_000,
  });
  return (
    <section className="panel" aria-label="Audit log">
      <div className="panel-head">
        <h2>Audit log</h2>
        <span className="hint">Every change: who, from where, what</span>
      </div>
      {!token ? (
        <Empty title="Admin only">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><LockIcon size={13} /> Entries name the address each change came from, so reading them needs the admin token.</span>
        </Empty>
      ) : audit.isPending ? (
        <Skeleton rows={3} />
      ) : audit.isError ? (
        <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
      ) : !audit.data.entries.length ? (
        <Empty title="No changes yet" />
      ) : (
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 640 }}>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Action</th>
                <th scope="col">By</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.entries.map((e, i) => (
                <tr key={`${e.time}-${i}`}>
                  <td className="mono muted">{formatClock(e.time)}</td>
                  <td><span className="chip mono">{e.action}</span></td>
                  <td className="mono">{e.actor}</td>
                  <td className="mono muted audit-details">{JSON.stringify(e.details)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ConfigPanels({ config }: { config: Config }) {
  return (
    <div className="split">
      <section className="panel narrow" aria-label="This server">
        <div className="panel-head"><h2>This server</h2></div>
        <div className="panel-body">
          <Kv k="Mode" v={<span className="mono">{config.env}</span>} />
          <Kv k="Admin API" v={config.admin_enabled ? "on (token set)" : "off: no SERVSCOPE_ADMIN_TOKEN"} />
          <Kv k="Failure scenarios" v={config.scenarios_enabled ? "available" : "switched off"} />
          <Kv k="Alert evaluation" v={config.alerts.enabled ? `every ${config.alerts.interval_s} s` : "off"} />
          <Kv k="Alerts look at data older than" v={`${config.alerts.settle_s} s (spans still in transit are not "silence")`} />
        </div>
      </section>
      <section className="panel narrow" aria-label="Health rules">
        <div className="panel-head"><h2>Health</h2><span className="hint">Over the last 5 minutes</span></div>
        <div className="panel-body">
          <Kv k="Degraded at" v={`${formatRate(config.health.degraded_error_rate)} errors or p95 ≥ ${formatMs(config.health.degraded_p95_ms)}`} />
          <Kv k="Down at" v={`${formatRate(config.health.down_error_rate)} errors`} />
          <Kv k="No data after" v={`${config.health.no_data_after_s} s of silence (never "healthy")`} />
          <Kv k="Percentiles need" v={`${config.latency.min_samples} requests`} />
        </div>
      </section>
      <section className="panel narrow" aria-label="Ingest limits">
        <div className="panel-head"><h2>Ingest limits</h2></div>
        <div className="panel-body">
          <Kv k="Largest export request" v={`${config.ingest.max_body_bytes / 1024 / 1024} MB`} />
          <Kv k="Concurrent writes" v={`${config.ingest.write_slots}; more wait 1 s, then get 503 + Retry-After`} />
          <Kv k="Attributes per span" v={`${config.ingest.max_attributes_bytes / 1024} KB, then truncated`} />
        </div>
      </section>
    </div>
  );
}

export function Settings() {
  const config = useQuery({ queryKey: ["config"], queryFn: ({ signal }) => api.config(signal) });
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <div className="sub">How this ServScope is set up. Secrets are never shown; most values come from SERVSCOPE_* variables.</div>
        </div>
      </header>
      {config.isPending ? (
        <div className="panel"><Skeleton rows={4} /></div>
      ) : config.isError ? (
        <div className="panel"><ErrorState error={config.error} onRetry={() => void config.refetch()} /></div>
      ) : (
        <>
          <ConfigPanels config={config.data} />
          <Retention key={JSON.stringify(config.data.retention_days)} days={config.data.retention_days} />
          <Audit />
        </>
      )}
    </>
  );
}
