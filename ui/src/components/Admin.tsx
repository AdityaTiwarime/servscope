import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from "react";

import { admin, ApiError } from "../api/client";
import { LockIcon, UnlockIcon } from "./Icons";

/**
 * The admin token is held in memory only: not localStorage, not a cookie. Reloading the page
 * locks the dashboard again, and nothing on disk can leak it. Every admin call sends it in
 * the Authorization header, which another website cannot make the browser add.
 */
interface AdminState {
  token: string | null;
  unlock: (token: string) => Promise<void>;
  lock: () => void;
  /** Wraps an admin call: if the server says the token is no longer valid, lock again. */
  run: <T>(action: (token: string) => Promise<T>) => Promise<T>;
  askToUnlock: () => void;
}

const AdminContext = createContext<AdminState | null>(null);

export function useAdmin(): AdminState {
  const state = useContext(AdminContext);
  if (!state) throw new Error("useAdmin needs an <AdminProvider>");
  return state;
}

export function AdminProvider({ children, initialToken = null }: { children: React.ReactNode; initialToken?: string | null }) {
  const [token, setToken] = useState<string | null>(initialToken);
  const [asking, setAsking] = useState(false);

  const unlock = useCallback(async (candidate: string) => {
    await admin.check(candidate); // throws ApiError(401) for a wrong token
    setToken(candidate);
  }, []);
  const lock = useCallback(() => setToken(null), []);
  const run = useCallback(
    async <T,>(action: (t: string) => Promise<T>): Promise<T> => {
      if (!token) {
        setAsking(true);
        throw new ApiError(401, "Admin token required", "unlock admin first");
      }
      try {
        return await action(token);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) setToken(null);
        throw error;
      }
    },
    [token],
  );
  const value = useMemo(
    () => ({ token, unlock, lock, run, askToUnlock: () => setAsking(true) }),
    [token, unlock, lock, run],
  );
  return (
    <AdminContext.Provider value={value}>
      {children}
      {asking && <UnlockDialog onClose={() => setAsking(false)} />}
    </AdminContext.Provider>
  );
}

function UnlockDialog({ onClose }: { onClose: () => void }) {
  const { unlock } = useAdmin();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const titleId = useId();

  useEffect(() => {
    input.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form
        className="dialog fade-in"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          unlock(value.trim())
            .then(onClose)
            .catch((err: unknown) => {
              setError(err instanceof ApiError ? (err.detail ? `${err.title}: ${err.detail}` : err.title) : "Could not check the token");
              setBusy(false);
            });
        }}
      >
        <div className="dialog-icon"><LockIcon size={20} /></div>
        <h2 id={titleId}>Unlock admin</h2>
        <p className="muted">
          Changing alert rules, settings and failure scenarios needs the admin token ServScope was started with
          (<code className="mono">SERVSCOPE_ADMIN_TOKEN</code>). It stays in this tab's memory only: reloading the page locks again.
        </p>
        <label className="field" style={error ? { borderColor: "var(--bad)" } : undefined}>
          <span className="visually-hidden">Admin token</span>
          <input
            ref={input}
            type="password"
            autoComplete="off"
            placeholder="Admin token"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={Boolean(error)}
            className="mono"
            style={{ width: "100%" }}
          />
        </label>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={!value.trim() || busy}>
            <UnlockIcon /> {busy ? "Checking…" : "Unlock"}
          </button>
        </div>
      </form>
    </div>
  );
}

/** The sidebar control: shows whether changes are possible and toggles it. */
export function AdminToggle() {
  const { token, lock, askToUnlock } = useAdmin();
  return token ? (
    <button type="button" className="btn admin-on" onClick={lock} title="Admin is unlocked in this tab. Click to forget the token.">
      <UnlockIcon /> Lock admin
    </button>
  ) : (
    <button type="button" className="btn" onClick={askToUnlock}>
      <LockIcon /> Unlock admin
    </button>
  );
}

/** A button for an admin action: when locked it asks to unlock instead of failing. */
export function AdminButton({
  children,
  onClick,
  className = "btn",
  disabled,
  label,
  compact = false,
}: {
  children: React.ReactNode;
  onClick: () => void;
  className?: string;
  disabled?: boolean;
  label?: string;
  compact?: boolean; // icon-only buttons: no lock glyph, the tooltip explains
}) {
  const { token, askToUnlock } = useAdmin();
  return (
    <button
      type="button"
      className={className}
      disabled={disabled}
      aria-label={label}
      onClick={token ? onClick : askToUnlock}
      title={token ? undefined : "Needs the admin token: click to unlock"}
    >
      {!token && !compact && <LockIcon size={13} />} {children}
    </button>
  );
}
