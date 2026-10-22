"""The scripted incident investigation (Milestone 5 acceptance test).

Against a running stack with traffic flowing, it does what an on-call engineer would:

1. checks the shop is healthy,
2. starts a failure scenario (default: payments timeout),
3. waits for the alert and times how long it took,
4. follows the evidence: incident -> a failing trace -> the span that failed -> its logs,
5. stops the scenario and times how long the incident takes to resolve,
6. prints the incident's timeline.

It checks each step and exits non-zero if any of them didn't happen, so CI can run it.
Every number it prints is measured on this run, not assumed.

    SERVSCOPE_ADMIN_TOKEN=... python scripts/incident_demo.py --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any

import httpx

# Which rule each scenario should trip, and on which service.
EXPECT = {
    "payments_timeout": ("Checkout errors", "gateway"),
    "payments_errors": ("Checkout errors", "gateway"),
    "payments_slow": ("Checkout latency (p95)", "gateway"),
    "inventory_unreachable": ("Service unavailable", "inventory"),
}
POLL_S = 3


class Failed(Exception):
    pass


class Demo:
    def __init__(self, api: str, token: str):
        self.http = httpx.Client(base_url=api, timeout=10)
        self.admin = {"Authorization": f"Bearer {token}"}
        self.t0 = time.monotonic()

    def say(self, text: str) -> None:
        print(f"[{time.monotonic() - self.t0:6.1f}s] {text}", flush=True)

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None) -> Any:
        r = self.http.post(path, json=body, headers=self.admin)
        if r.status_code >= 400:
            raise Failed(f"POST {path} -> {r.status_code}: {r.text}")
        return r.json()

    def wait(self, what: str, limit_s: float, check: Any) -> Any:
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(POLL_S)
        raise Failed(f"{what}: not within {limit_s:.0f} s")


def healthy_gateway(demo: Demo) -> bool:
    services = {s["service"]: s for s in demo.get("/api/services")["services"]}
    gw = services.get("gateway")
    return bool(gw and gw["health"] == "healthy" and gw["requests"] > 0)


def run(demo: Demo, scenario: str, duration_s: int, open_limit: float, resolve_limit: float) -> int:
    rule_name, service = EXPECT[scenario]
    config = demo.get("/api/config")
    if not config["scenarios_enabled"]:
        raise Failed(
            "scenarios are switched off: set SERVSCOPE_ENV=demo and "
            "SERVSCOPE_FAILURE_INJECTION=true (see README, 'Failure scenarios')"
        )
    interval = config["alerts"]["interval_s"]
    demo.say(f"alerts are evaluated every {interval:g} s, on data settled for "
             f"{config['alerts']['settle_s']:g} s")  # fmt: skip

    # 1. A healthy starting point, so we know the alert is caused by the scenario.
    demo.say("waiting for the shop to look healthy (traffic must be running)...")
    demo.wait("gateway healthy", 300, lambda: healthy_gateway(demo))

    def none_open() -> bool:
        return not [
            i for i in demo.get("/api/incidents", state="open")["incidents"]
            if (i["rule_name"], i["service"]) == (rule_name, service)
        ]  # fmt: skip

    if not none_open():  # left over from an earlier run: let it resolve first
        demo.say(f"an earlier {rule_name!r} incident is still open; waiting for it to resolve")
        demo.wait("earlier incident resolved", resolve_limit, none_open)
    demo.say("gateway is healthy")

    # 2. Break something.
    started_wall = datetime.now().astimezone()
    start = time.monotonic()
    demo.post(f"/api/scenarios/{scenario}/start", {"duration_s": duration_s})
    demo.say(f"started scenario {scenario!r} for at most {duration_s} s")

    # 3. The alert.
    def opened() -> dict[str, Any] | None:
        for i in demo.get("/api/incidents", state="open")["incidents"]:
            fresh = datetime.fromisoformat(i["opened_at"]) >= started_wall
            if fresh and (i["rule_name"], i["service"]) == (rule_name, service):
                return dict(i)
        return None

    incident = demo.wait(f"alert {rule_name!r} on {service}", open_limit, opened)
    fired_after = time.monotonic() - start
    demo.say(f"ALERT: {rule_name} on {service}, incident #{incident['id']}, "
             f"{fired_after:.0f} s after the scenario started")  # fmt: skip

    # 4. Follow the evidence.
    samples = incident["sample_trace_ids"]
    if not samples:
        raise Failed("the incident links no traces")
    demo.say(f"the incident links {len(samples)} trace(s); opening {samples[0]}")
    trace = demo.get(f"/api/traces/{samples[0]}")
    failing = [s for s in trace["spans"] if s["status"] == "error"]
    for s in failing:
        why = s["error_type"] or s["status_message"] or (s["http_status"] or "")
        demo.say(f"  error span: {s['service']:<9} {s['operation']:<22} {why}")
    slowest = next(s for s in trace["spans"] if s["span_id"] == trace["slowest_span_id"])
    demo.say(f"  slowest span: {slowest['service']} {slowest['operation']} "
             f"({slowest['duration_ns'] / 1e6:.0f} ms)")  # fmt: skip
    if scenario == "payments_slow":
        # A latency alert's evidence is the slowest requests: slow, not failing.
        if slowest["service"] != "payments" or slowest["duration_ns"] < 1.4e9:
            raise Failed("the slowest span of a linked trace should be payments, over 1.4 s")
    elif not failing:
        raise Failed("the linked trace has no failing span")
    severity = "INFO" if scenario == "payments_slow" else "WARN"
    logs = demo.get("/api/logs", trace_id=samples[0], severity=severity)["logs"]
    for line in logs[:6]:
        demo.say(f"  log {line['severity']:<5} {line['service']:<9} {line['message']}")
    if scenario != "inventory_unreachable" and not logs:
        raise Failed("no log lines are linked to the trace")

    # 5. Fix it, and watch the incident resolve on its own.
    demo.post("/api/scenarios/stop")
    stop = time.monotonic()
    demo.say("stopped the scenario; waiting for the incident to resolve")

    def resolved() -> dict[str, Any] | None:
        detail: dict[str, Any] = demo.get(f"/api/incidents/{incident['id']}")
        return detail if detail["state"] == "resolved" else None

    detail = demo.wait("incident resolved", resolve_limit, resolved)
    demo.say(f"RESOLVED {time.monotonic() - stop:.0f} s after the scenario stopped")

    # 6. The story, as ServScope recorded it.
    print("\nincident timeline:")
    for e in detail["events"]:
        value = "" if e["value"] is None else f" value={e['value']:.3g}"
        traces = f" traces={len(e['sample_trace_ids'])}" if e["sample_trace_ids"] else ""
        print(f"  {e['time'][11:19]}  {e['type']:<15}{value}{traces}  {e['detail']}")

    duplicates = [
        i for i in demo.get("/api/incidents", limit=100)["incidents"]
        if (i["rule_name"], i["service"]) == (rule_name, service)
        and datetime.fromisoformat(i["opened_at"]) >= started_wall
    ]  # fmt: skip
    if len(duplicates) != 1:
        raise Failed(f"expected one incident for this outage, found {len(duplicates)}")
    print(f"\nPASS: alert after {fired_after:.0f} s, one incident, evidence linked, "
          f"resolved {time.monotonic() - stop:.0f} s after the fix")  # fmt: skip
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--scenario", choices=sorted(EXPECT), default="payments_timeout")
    parser.add_argument("--duration", type=int, default=300, help="scenario cap, seconds")
    parser.add_argument("--open-limit", type=float, default=240)
    parser.add_argument("--resolve-limit", type=float, default=420)
    args = parser.parse_args()
    token = os.environ.get("SERVSCOPE_ADMIN_TOKEN")
    if not token:
        parser.error("set SERVSCOPE_ADMIN_TOKEN (the same value ServScope runs with)")
    demo = Demo(args.api, token)
    try:
        return run(demo, args.scenario, args.duration, args.open_limit, args.resolve_limit)
    except Failed as exc:
        demo.say(f"FAIL: {exc}")
        try:  # never leave a fault switched on
            demo.post("/api/scenarios/stop")
        except Exception:  # noqa: S110  # best effort; the shop's own timer ends it anyway
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
