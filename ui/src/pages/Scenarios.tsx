import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { admin, api } from "../api/client";
import type { Config, Scenario, ScenariosResponse } from "../api/types";
import { AdminButton, useAdmin } from "../components/Admin";
import { AlertIcon, CheckIcon, LockIcon, PlayIcon, StopIcon } from "../components/Icons";
import { ErrorState, Skeleton } from "../components/States";
import { formatCountdown } from "../lib/alerts";

const DURATIONS = [2, 5, 10]; // minutes; the server caps every scenario at 10

/** Counts down locally between refreshes, from the remaining time the shop itself reported. */
function useCountdown(remaining: number | undefined, updatedAt: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (remaining === undefined) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [remaining]);
  return remaining === undefined ? 0 : remaining - (now - updatedAt) / 1000;
}

function Locks({ config }: { config: Config }) {
  const { token } = useAdmin();
  const locks = [
    { ok: config.env === "demo", text: "SERVSCOPE_ENV=demo", why: "never in production: ServScope refuses to start with injection there" },
    { ok: config.scenarios_enabled, text: "SERVSCOPE_FAILURE_INJECTION=true", why: "off by default; without it these controls don't exist on the server" },
    { ok: Boolean(token), text: "Admin token", why: "every start and stop needs it, and is written to the audit log" },
  ];
  return (
    <ul className="locks" aria-label="Safety locks">
      {locks.map((l) => (
        <li key={l.text} className={l.ok ? "open" : "closed"}>
          {l.ok ? <CheckIcon size={15} /> : <LockIcon size={15} />}
          <span><span className="mono">{l.text}</span> <span className="muted">· {l.why}</span></span>
        </li>
      ))}
    </ul>
  );
}

function ScenarioCard({
  scenario,
  active,
  busy,
  onStart,
}: {
  scenario: Scenario;
  active: boolean;
  busy: boolean;
  onStart: (minutes: number) => void;
}) {
  const [minutes, setMinutes] = useState(5);
  return (
    <article className={`scenario-card ${active ? "running" : ""}`} aria-label={scenario.title}>
      <div className="scenario-top">
        <h3>{scenario.title}</h3>
        <span className="chip">{scenario.target}</span>
        {active && <span className="pill down">running</span>}
      </div>
      <p>{scenario.what_happens}</p>
      <div className="look-for">
        <div className="chart-title">What ServScope should show</div>
        {scenario.what_to_look_for}
      </div>
      <div className="scenario-actions">
        <label className="field" style={{ height: 34 }}>
          <span className="visually-hidden">Duration for {scenario.title}</span>
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
            {DURATIONS.map((m) => <option key={m} value={m}>for {m} min</option>)}
          </select>
        </label>
        <AdminButton className="btn btn-primary" disabled={busy || active} onClick={() => onStart(minutes)} label={`Start ${scenario.title}`}>
          <PlayIcon size={13} /> {active ? "Running" : "Start"}
        </AdminButton>
      </div>
    </article>
  );
}

function Running({ data, updatedAt, onStop, stopping }: { data: ScenariosResponse; updatedAt: number; onStop: () => void; stopping: boolean }) {
  const left = useCountdown(data.active?.remaining_s, updatedAt);
  if (!data.active) return null;
  const title = data.scenarios.find((s) => s.name === data.active?.name)?.title ?? data.active.name;
  return (
    <section className="banner fade-in" aria-label="Running scenario">
      <span className="icon"><AlertIcon size={18} /></span>
      <div className="text">
        <div className="title">{title} is running · <span className="mono">{formatCountdown(left)}</span> left</div>
        <div className="detail">
          It switches itself off when the time runs out, even if ServScope stops. Watch it on the{" "}
          <Link to="/">Overview</Link> and in <Link to="/alerts">Alerts</Link>.
        </div>
      </div>
      <AdminButton className="btn btn-danger" onClick={onStop} disabled={stopping}>
        <StopIcon size={13} /> {stopping ? "Stopping…" : "Stop now"}
      </AdminButton>
    </section>
  );
}

export function Scenarios() {
  const { run } = useAdmin();
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["config"], queryFn: ({ signal }) => api.config(signal) });
  const enabled = config.data?.scenarios_enabled ?? false;
  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: ({ signal }) => api.scenarios(signal),
    enabled,
    refetchInterval: 5_000,
  });
  const onDone = (data: ScenariosResponse) => client.setQueryData(["scenarios"], data);
  const start = useMutation({
    mutationFn: ({ name, minutes }: { name: string; minutes: number }) => run((t) => admin.startScenario(t, name, minutes * 60)),
    onSuccess: onDone,
  });
  const stop = useMutation({ mutationFn: () => run((t) => admin.stopScenarios(t)), onSuccess: onDone });
  const failure = start.error ?? stop.error;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Demo scenarios</h1>
          <div className="sub">Break the demo shop on purpose, safely, and watch the failure appear in traces, metrics, logs and alerts</div>
        </div>
      </header>

      {config.isPending ? (
        <div className="panel"><Skeleton rows={3} /></div>
      ) : config.isError ? (
        <div className="panel"><ErrorState error={config.error} onRetry={() => void config.refetch()} /></div>
      ) : (
        <>
          <section className="panel" aria-label="Safety">
            <div className="panel-head">
              <h2>Safety locks</h2>
              <span className="hint">All three must be open to start a scenario</span>
            </div>
            <div className="panel-body"><Locks config={config.data} /></div>
            {!enabled && (
              <div className="panel-body">
                <p className="hint-text" style={{ marginTop: 0 }}>
                  Failure injection is switched off on this server, so there is nothing to start. To use it on a local demo,
                  set these in <span className="mono">.env</span> and restart: <span className="mono">SERVSCOPE_ENV=demo</span>,{" "}
                  <span className="mono">SERVSCOPE_FAILURE_INJECTION=true</span>, <span className="mono">SERVSCOPE_ADMIN_TOKEN</span> and{" "}
                  <span className="mono">SERVSCOPE_FAULT_TOKEN</span>.
                </p>
              </div>
            )}
          </section>

          {enabled && (scenarios.isPending ? (
            <div className="panel"><Skeleton rows={3} /></div>
          ) : scenarios.isError && !scenarios.data ? (
            <div className="panel"><ErrorState error={scenarios.error} onRetry={() => void scenarios.refetch()} /></div>
          ) : scenarios.data ? (
            <>
              <Running data={scenarios.data} updatedAt={scenarios.dataUpdatedAt} onStop={() => stop.mutate()} stopping={stop.isPending} />
              {failure && <div className="form-error" role="alert">{failure.message}</div>}
              {Object.entries(scenarios.data.targets).some(([, s]) => s === "unreachable") && (
                <div className="form-error" role="status">
                  Can't reach the fault switch on{" "}
                  {Object.entries(scenarios.data.targets).filter(([, s]) => s === "unreachable").map(([t]) => t).join(", ")}: is the demo shop running?
                </div>
              )}
              <section className="scenario-grid" aria-label="Scenarios">
                {scenarios.data.scenarios.map((s) => (
                  <ScenarioCard
                    key={s.name}
                    scenario={s}
                    active={scenarios.data.active?.name === s.name}
                    busy={start.isPending}
                    onStart={(minutes) => start.mutate({ name: s.name, minutes })}
                  />
                ))}
              </section>
            </>
          ) : null)}
        </>
      )}
    </>
  );
}
