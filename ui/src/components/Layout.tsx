import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate, useSearchParams } from "react-router-dom";

import { parseRange, RANGES, type Range } from "../lib/time";
import {
  BellIcon,
  GearIcon,
  Logo,
  LogsIcon,
  MoonIcon,
  OverviewIcon,
  PlayIcon,
  SearchIcon,
  ServicesIcon,
  SunIcon,
  TracesIcon,
} from "./Icons";

const NAV = [
  { to: "/", label: "Overview", icon: OverviewIcon, end: true },
  { to: "/services", label: "Services", icon: ServicesIcon },
  { to: "/traces", label: "Traces", icon: TracesIcon },
  { to: "/logs", label: "Logs", icon: LogsIcon },
];
const LATER = [
  { label: "Alerts", icon: BellIcon },
  { label: "Demo scenarios", icon: PlayIcon },
  { label: "Settings", icon: GearIcon },
];

type Theme = "dark" | "light";

function readTheme(): Theme {
  try {
    const saved = localStorage.getItem("servscope.theme");
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    // storage blocked: use the default
  }
  return "dark"; // dark-first by design; the toggle switches and remembers light

}

/** The time range lives in the URL (?range=1h), so a shared link shows the same view. */
export function useRange(): [Range, (r: Range) => void] {
  const [params, setParams] = useSearchParams();
  const range = parseRange(params.get("range"));
  const set = (r: Range) =>
    setParams((p) => {
      const next = new URLSearchParams(p);
      next.set("range", r);
      return next;
    });
  return [range, set];
}

export function RangePicker() {
  const [range, setRange] = useRange();
  return (
    <div className="segmented" role="group" aria-label="Time range">
      {RANGES.map((r) => (
        <button key={r} type="button" aria-pressed={r === range} onClick={() => setRange(r)}>
          {r}
        </button>
      ))}
    </div>
  );
}

export function TraceSearch() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [invalid, setInvalid] = useState(false);
  return (
    <form
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        const id = value.trim().toLowerCase();
        if (/^[0-9a-f]{32}$/.test(id)) navigate(`/traces/${id}`);
        else setInvalid(true);
      }}
    >
      <label className="field" style={invalid ? { borderColor: "var(--bad)" } : undefined}>
        <SearchIcon size={14} />
        <span className="visually-hidden">Find a trace by its 32-character ID</span>
        <input
          type="search"
          placeholder="Find trace by ID"
          value={value}
          aria-invalid={invalid}
          onChange={(e) => {
            setValue(e.target.value);
            setInvalid(false);
          }}
          style={{ width: 200 }}
          className="mono"
        />
      </label>
    </form>
  );
}

export function Layout() {
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [params] = useSearchParams();
  const range = params.get("range");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("servscope.theme", theme);
    } catch {
      // not saved: fine, it is only a preference
    }
  }, [theme]);

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="brand"><Logo /> ServScope</div>
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={range ? `${to}?range=${range}` : to} end={end} className="navlink">
            <Icon /> {label}
          </NavLink>
        ))}
        {LATER.map(({ label, icon: Icon }) => (
          <span key={label} className="navlink disabled" aria-disabled="true" title="Arrives in Milestone 5">
            <Icon /> {label} <span className="soon">Soon</span>
          </span>
        ))}
        <div className="sidebar-foot">
          <button
            type="button"
            className="btn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            {theme === "dark" ? <SunIcon /> : <MoonIcon />} {theme === "dark" ? "Light" : "Dark"} theme
          </button>
          <span className="note">Read-only view. Keep ServScope on localhost or a private network.</span>
        </div>
      </nav>
      <main className="main" id="main">
        <Outlet />
      </main>
    </div>
  );
}
