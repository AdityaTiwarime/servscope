import { useId, useState } from "react";

import { ApiError } from "../api/client";
import type { AlertRule, RuleInput, RuleKind } from "../api/types";
import { KINDS } from "../lib/alerts";

const WINDOWS = [1, 2, 3, 5, 10, 15, 30, 60]; // minutes; metrics are kept per minute
const DEFAULT_THRESHOLD: Record<RuleKind, number> = { error_rate: 0.1, p95_latency: 1000, unavailable: 0, failure_rise: 3 };
const EMPTY: RuleInput = {
  name: "",
  kind: "error_rate",
  service: null,
  threshold: 0.1,
  window_s: 120,
  for_s: 30,
  recover_s: 60,
  enabled: true,
};

/** Create or edit a rule. The server validates again; its explanation is shown as is. */
export function RuleForm({
  rule,
  services,
  onSave,
  onCancel,
}: {
  rule?: AlertRule;
  services: string[];
  onSave: (input: RuleInput) => Promise<unknown>;
  onCancel: () => void;
}) {
  const start: RuleInput = rule ? { ...rule } : EMPTY;
  const [form, setForm] = useState<RuleInput>(start);
  const [threshold, setThreshold] = useState(String(KINDS[start.kind].toInput(start.threshold)));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const id = useId();
  const kind = KINDS[form.kind];
  const set = <K extends keyof RuleInput>(key: K, value: RuleInput[K]) => setForm((f) => ({ ...f, [key]: value }));

  return (
    <form
      className="rule-form fade-in"
      aria-label={rule ? `Edit ${rule.name}` : "New alert rule"}
      onSubmit={(e) => {
        e.preventDefault();
        const number = Number(threshold);
        if (form.kind !== "unavailable" && !Number.isFinite(number)) {
          setError("The threshold must be a number");
          return;
        }
        setBusy(true);
        setError(null);
        onSave({ ...form, name: form.name.trim(), threshold: kind.fromInput(number) }).catch((err: unknown) => {
          setError(err instanceof ApiError ? (err.detail ? `${err.title}: ${err.detail}` : err.title) : "Could not save");
          setBusy(false);
        });
      }}
    >
      <div className="form-grid">
        <label className="form-row wide-row" htmlFor={`${id}-name`}>
          <span>Name</span>
          <input id={`${id}-name`} className="input" required maxLength={80} value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Orders errors" />
        </label>
        <label className="form-row" htmlFor={`${id}-kind`}>
          <span>Measure</span>
          <select
            id={`${id}-kind`}
            className="input"
            value={form.kind}
            onChange={(e) => {
              const next = e.target.value as RuleKind;
              set("kind", next);
              setThreshold(String(KINDS[next].toInput(DEFAULT_THRESHOLD[next])));
            }}
          >
            {(Object.keys(KINDS) as RuleKind[]).map((k) => (
              <option key={k} value={k}>{KINDS[k].label}</option>
            ))}
          </select>
        </label>
        <label className="form-row" htmlFor={`${id}-service`}>
          <span>Service</span>
          <select id={`${id}-service`} className="input" value={form.service ?? ""} onChange={(e) => set("service", e.target.value || null)}>
            <option value="">Every service</option>
            {[...new Set([...services, ...(form.service ? [form.service] : [])])].sort().map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        {form.kind !== "unavailable" && (
          <div className="form-row">
            <label htmlFor={`${id}-threshold`}>Above<span className="visually-hidden"> ({kind.unit})</span></label>
            <span className="input with-unit">
              <input id={`${id}-threshold`} inputMode="decimal" required value={threshold} onChange={(e) => setThreshold(e.target.value)} />
              <span className="muted" aria-hidden="true">{kind.unit}</span>
            </span>
          </div>
        )}
        <label className="form-row" htmlFor={`${id}-window`}>
          <span>{form.kind === "unavailable" ? "Silent for" : "Over the last"}</span>
          <select id={`${id}-window`} className="input" value={form.window_s} onChange={(e) => set("window_s", Number(e.target.value))}>
            {WINDOWS.map((m) => <option key={m} value={m * 60}>{m} min</option>)}
          </select>
        </label>
        <label className="form-row" htmlFor={`${id}-for`}>
          <span>Fire after (s)</span>
          <input id={`${id}-for`} className="input" type="number" min={0} max={3600} value={form.for_s} onChange={(e) => set("for_s", Number(e.target.value))} />
        </label>
        <label className="form-row" htmlFor={`${id}-recover`}>
          <span>Resolve after (s)</span>
          <input id={`${id}-recover`} className="input" type="number" min={0} max={3600} value={form.recover_s} onChange={(e) => set("recover_s", Number(e.target.value))} />
        </label>
      </div>
      <p className="hint-text">
        {kind.help} An alert fires only after the condition has held for "fire after", and resolves only after it has been
        clear for "resolve after", so one bad minute doesn't page anyone and a value hovering at the line doesn't flap.
      </p>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="dialog-actions">
        <label className="check">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Enabled
        </label>
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : rule ? "Save rule" : "Create rule"}</button>
      </div>
    </form>
  );
}
