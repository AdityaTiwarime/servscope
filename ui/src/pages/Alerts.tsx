import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { admin, api } from "../api/client";
import type { AlertRule, Incident, RuleInput } from "../api/types";
import { AdminButton, useAdmin } from "../components/Admin";
import { AlertIcon, CheckIcon, ClockIcon, PencilIcon, PlusIcon, TrashIcon } from "../components/Icons";
import { RuleForm } from "../components/RuleForm";
import { Empty, ErrorState, Skeleton, StaleBanner } from "../components/States";
import { formatValue, incidentDuration, KINDS, STATE_TEXT } from "../lib/alerts";
import { formatAgo, formatClock } from "../lib/format";

const REFRESH_MS = 5_000;

export function useOpenIncidents() {
  return useQuery({
    queryKey: ["incidents", "open"],
    queryFn: ({ signal }) => api.incidents({ state: "open", limit: 50 }, signal),
    refetchInterval: REFRESH_MS,
  });
}

/** One open incident, as a card: what fired, where, how bad, since when, and what to do. */
export function IncidentCard({ incident }: { incident: Incident }) {
  const { run } = useAdmin();
  const client = useQueryClient();
  const ack = useMutation({
    mutationFn: () => run((t) => admin.acknowledge(t, incident.id)),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["incidents"] }),
  });
  return (
    <article className={`incident-card fade-in ${incident.acknowledged_at ? "acked" : ""}`} aria-label={`${incident.rule_name} on ${incident.service}`}>
      <div className="incident-stripe" aria-hidden="true" />
      <div className="incident-main">
        <div className="incident-title">
          <span className="firing-dot" aria-hidden="true" />
          <Link to={`/alerts/incidents/${incident.id}`} className="row-link">{incident.rule_name}</Link>
          <span className="chip">{incident.service}</span>
          {incident.acknowledged_at && <span className="status">acknowledged</span>}
        </div>
        <div className="incident-facts">
          <span><ClockIcon size={13} /> open {incidentDuration(incident)} · since {formatClock(incident.opened_at)}</span>
          <span>
            peak <strong className="mono bad-text">{formatValue(incident.kind, incident.peak_value)}</strong>
            {incident.kind !== "unavailable" && <> · limit <span className="mono">{formatValue(incident.kind, incident.threshold)}</span></>}
          </span>
          <span>{incident.sample_trace_ids.length} linked trace{incident.sample_trace_ids.length === 1 ? "" : "s"}</span>
        </div>
        {ack.isError && <div className="form-error" role="alert">{ack.error.message}</div>}
      </div>
      <div className="incident-actions">
        {!incident.acknowledged_at && (
          <AdminButton onClick={() => ack.mutate()} disabled={ack.isPending}>
            {ack.isPending ? "…" : "Acknowledge"}
          </AdminButton>
        )}
        <Link className="btn btn-danger" to={`/alerts/incidents/${incident.id}`}>Investigate</Link>
      </div>
    </article>
  );
}

/**
 * Open incidents, grouped by service. Each rule opens at most one incident per service, but
 * one outage can trip several rules at once (errors, latency, failures rising, all at the
 * gateway): shown together, they read as one problem with several symptoms.
 */
export function IncidentGroups({ incidents }: { incidents: Incident[] }) {
  const groups = new Map<string, Incident[]>();
  for (const i of [...incidents].sort((a, b) => a.opened_at.localeCompare(b.opened_at))) {
    groups.set(i.service, [...(groups.get(i.service) ?? []), i]);
  }
  return (
    <>
      {[...groups.entries()].map(([service, list]) =>
        list.length === 1 && list[0] ? (
          <IncidentCard key={service} incident={list[0]} />
        ) : (
          <section key={service} className="incident-group" aria-label={`${list.length} alerts on ${service}`}>
            <div className="group-head">
              <span className="chip">{service}</span> {list.length} alerts firing: likely one problem, several symptoms
            </div>
            {list.map((i) => <IncidentCard key={i.id} incident={i} />)}
          </section>
        ),
      )}
    </>
  );
}

function StateChips({ rule }: { rule: AlertRule }) {
  if (!rule.enabled) return <span className="muted">disabled</span>;
  if (!rule.states.length) return <span className="muted">waiting for the first check</span>;
  return (
    <span className="chips">
      {rule.states.map((s) => (
        <span key={s.service} className={`rule-state ${s.state}`} title={`${s.state} since ${formatClock(s.since)}; last value ${formatValue(rule.kind, s.value)}`}>
          {s.service} · {STATE_TEXT[s.state]}
        </span>
      ))}
    </span>
  );
}

function Rules() {
  const { run } = useAdmin();
  const client = useQueryClient();
  const rules = useQuery({ queryKey: ["rules"], queryFn: ({ signal }) => api.rules(signal), refetchInterval: REFRESH_MS });
  const known = useQuery({
    queryKey: ["services", "known"],
    queryFn: ({ signal }) => {
      const to = new Date();
      return api.services({ from: new Date(to.getTime() - 24 * 3600_000).toISOString(), to: to.toISOString() }, signal);
    },
    staleTime: 60_000,
  });
  const [editing, setEditing] = useState<AlertRule | "new" | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["rules"] });
  const save = async (input: RuleInput) => {
    if (editing === "new") await run((t) => admin.createRule(t, input));
    else if (editing) await run((t) => admin.updateRule(t, editing.id, input));
    setEditing(null);
    await refresh();
  };
  const remove = useMutation({
    mutationFn: (rule: AlertRule) => run((t) => admin.deleteRule(t, rule.id)),
    onSuccess: () => void client.invalidateQueries(),
  });
  const services = known.data?.services.map((s) => s.service) ?? [];

  return (
    <section className="panel" aria-label="Alert rules">
      <div className="panel-head">
        <h2>Rules</h2>
        <span className="hint">Checked every 15 s. Each service a rule covers has its own state.</span>
        <span className="right">
          <AdminButton onClick={() => setEditing("new")}><PlusIcon /> New rule</AdminButton>
        </span>
      </div>
      {editing && (
        <div className="panel-body">
          <RuleForm rule={editing === "new" ? undefined : editing} services={services} onSave={save} onCancel={() => setEditing(null)} />
        </div>
      )}
      {remove.isError && <div className="panel-body"><div className="form-error" role="alert">{remove.error.message}</div></div>}
      {rules.isPending ? (
        <Skeleton rows={3} />
      ) : rules.isError && !rules.data ? (
        <ErrorState error={rules.error} onRetry={() => void rules.refetch()} />
      ) : !rules.data?.rules.length ? (
        <Empty title="No alert rules">Without rules nothing will ever alert. Unlock admin and add one.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 860 }}>
            <thead>
              <tr>
                <th scope="col">Rule</th>
                <th scope="col">Condition</th>
                <th scope="col">Watching</th>
                <th scope="col">Now</th>
                <th scope="col"><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rules.data.rules.map((r) => (
                <tr key={r.id} className={r.enabled ? "" : "dimmed"}>
                  <td>
                    <div className="row-link">{r.name}</div>
                    <div className="muted" style={{ fontSize: 11 }}>{KINDS[r.kind].label}</div>
                  </td>
                  <td style={{ whiteSpace: "normal", minWidth: 220 }}>
                    {r.description}
                    {r.recover_s > 0 && <div className="muted" style={{ fontSize: 11 }}>resolves after {r.recover_s} s clear</div>}
                  </td>
                  <td>{r.service ?? <span className="muted">every service</span>}</td>
                  <td style={{ whiteSpace: "normal" }}><StateChips rule={r} /></td>
                  <td className="num">
                    <span className="row-actions">
                      <AdminButton compact className="btn btn-icon" label={`Edit ${r.name}`} onClick={() => setEditing(r)}><PencilIcon /></AdminButton>
                      <AdminButton
                        compact
                        className="btn btn-icon"
                        label={`Delete ${r.name}`}
                        disabled={remove.isPending}
                        onClick={() => {
                          if (window.confirm(`Delete "${r.name}"? Its open incidents will be resolved; their history stays.`)) remove.mutate(r);
                        }}
                      >
                        <TrashIcon />
                      </AdminButton>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function History() {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;
  const history = useQuery({
    queryKey: ["incidents", "resolved", cursor],
    queryFn: ({ signal }) => api.incidents({ state: "resolved", limit: 10, cursor }, signal),
    refetchInterval: REFRESH_MS * 3,
  });
  return (
    <section className="panel" aria-label="Incident history">
      <div className="panel-head">
        <h2>History</h2>
        <span className="hint">Resolved incidents, newest first, kept 90 days</span>
      </div>
      {history.isPending ? (
        <Skeleton rows={3} />
      ) : history.isError && !history.data ? (
        <ErrorState error={history.error} onRetry={() => void history.refetch()} />
      ) : !history.data?.incidents.length ? (
        <Empty title="No resolved incidents yet">Incidents appear here once they recover.</Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="data" style={{ minWidth: 640 }}>
              <thead>
                <tr>
                  <th scope="col">Opened</th>
                  <th scope="col">Rule</th>
                  <th scope="col">Service</th>
                  <th scope="col" className="num">Lasted</th>
                  <th scope="col" className="num">Peak</th>
                </tr>
              </thead>
              <tbody>
                {history.data.incidents.map((i) => (
                  <tr key={i.id}>
                    <td className="mono muted">{formatAgo(i.opened_at)} · {formatClock(i.opened_at)}</td>
                    <td><Link className="row-link" to={`/alerts/incidents/${i.id}`}>{i.rule_name}</Link></td>
                    <td><span className="chip">{i.service}</span></td>
                    <td className="num mono">{incidentDuration(i)}</td>
                    <td className="num mono">{formatValue(i.kind, i.peak_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button type="button" className="btn" disabled={cursors.length < 2} onClick={() => setCursors((c) => c.slice(0, -1))}>Newer</button>
            <button type="button" className="btn" disabled={!history.data.next_cursor} onClick={() => setCursors((c) => [...c, history.data.next_cursor])}>Older</button>
          </div>
        </>
      )}
    </section>
  );
}

export function Alerts() {
  const open = useOpenIncidents();
  const incidents = open.data?.incidents ?? [];
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Alerts</h1>
          <div className="sub">Rules turn metrics into incidents; one incident per rule and service, however long it lasts</div>
        </div>
        <div className="toolbar">
          <span className="live">
            <span className={`live-dot ${open.isError ? "stale" : "pulse"}`} />
            {open.isError ? "Updates paused" : `Live · every ${REFRESH_MS / 1000} s`}
          </span>
        </div>
      </header>

      {open.isError && open.data && <StaleBanner error={open.error} updatedAt={open.dataUpdatedAt} />}

      <section aria-label="Open incidents" className="incident-list">
        {open.isPending ? (
          <div className="panel"><Skeleton rows={2} /></div>
        ) : open.isError && !open.data ? (
          <div className="panel"><ErrorState error={open.error} onRetry={() => void open.refetch()} /></div>
        ) : incidents.length ? (
          <>
            <div className="section-label"><AlertIcon size={14} /> {incidents.length} open incident{incidents.length === 1 ? "" : "s"}</div>
            <IncidentGroups incidents={incidents} />
          </>
        ) : (
          <div className="banner ok fade-in">
            <span className="icon"><CheckIcon size={18} /></span>
            <div className="text">
              <div className="title">No open incidents</div>
              <div className="detail">Every rule is within its limits, or waiting for enough data to judge.</div>
            </div>
          </div>
        )}
      </section>

      <Rules />
      <History />
    </>
  );
}
