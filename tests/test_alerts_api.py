"""Milestone 5: the alert-rule, incident, settings and scenario APIs, and their security.

Scenario tests talk to the demo shop's REAL fault switches (payments and orders, run
in-process), so they check the whole path: ServScope's locks, the token it sends, the
duration cap, and what the shop actually switches on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import ADMIN, ADMIN_TOKEN, settings
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from demo_services import orders, payments
from demo_services.faults import Faults
from demo_services.telemetry import Exporters
from servscope.config import Settings

FAULT_TOKEN = "test-fault-token-0123456789"
WRONG = {"Authorization": "Bearer not-the-admin-token-at-all"}
GOOD_RULE = {
    "name": "Orders errors",
    "kind": "error_rate",
    "service": "orders",
    "threshold": 0.2,
    "window_s": 180,
    "for_s": 60,
    "recover_s": 60,
}


@pytest.fixture
def admin_client(make_client: Any) -> Any:
    return make_client(admin_token=SecretStr(ADMIN_TOKEN))


# --- admin authentication ------------------------------------------------------------


async def test_reads_are_open_and_list_the_default_rules(admin_client: Any) -> None:
    async with admin_client as c:
        r = await c.get("/api/alert-rules")
    assert r.status_code == 200
    rules = {rule["name"]: rule for rule in r.json()["rules"]}
    assert set(rules) == {
        "Checkout errors",
        "Checkout latency (p95)",
        "Service unavailable",
        "Checkout failures rising",
    }
    assert rules["Checkout errors"]["description"] == "error rate above 10% over 2 min, for 30 s"
    assert rules["Service unavailable"]["service"] is None  # every service


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/alert-rules", GOOD_RULE),
        ("PUT", "/api/alert-rules/1", GOOD_RULE),
        ("DELETE", "/api/alert-rules/1", None),
        ("POST", "/api/incidents/1/ack", None),
        ("PUT", "/api/settings/retention", {"spans": 3, "logs": 3, "rollups": 10}),
        ("GET", "/api/audit", None),
    ],
)
async def test_every_change_needs_the_admin_token(
    admin_client: Any, method: str, path: str, body: Any
) -> None:
    async with admin_client as c:
        for headers in ({}, WRONG, {"Authorization": ADMIN_TOKEN}):  # none, wrong, no "Bearer"
            r = await c.request(method, path, json=body, headers=headers)
            assert r.status_code == 401, (headers, r.text)
            assert r.headers["WWW-Authenticate"] == "Bearer"
            assert r.headers["content-type"] == "application/problem+json"
        rules = (await c.get("/api/alert-rules")).json()["rules"]
    assert len(rules) == 4  # nothing was changed by the refused calls


async def test_without_an_admin_token_configured_admin_is_off(make_client: Any) -> None:
    async with make_client() as c:  # no SERVSCOPE_ADMIN_TOKEN
        r = await c.post("/api/alert-rules", json=GOOD_RULE, headers=ADMIN)
        config = (await c.get("/api/config")).json()
    assert r.status_code == 403
    assert config["admin_enabled"] is False


def test_short_tokens_are_refused_at_startup(clean_db: str) -> None:
    with pytest.raises(ValidationError, match="at least 16 characters"):
        settings(clean_db, admin_token=SecretStr("short"))


def test_an_empty_token_means_admin_is_off(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVSCOPE_ADMIN_TOKEN", "")  # what Compose passes for ${VAR:-}
    monkeypatch.setenv("SERVSCOPE_FAULT_TOKEN", "")
    s = Settings(database_url=clean_db, ingest_token=SecretStr("x" * 20))
    assert s.admin_token is None and s.fault_token is None


async def test_config_never_contains_secrets(admin_client: Any) -> None:
    async with admin_client as c:
        r = await c.get("/api/config")
    assert r.status_code == 200
    assert ADMIN_TOKEN not in r.text and "test-ingest-token" not in r.text
    assert r.json()["admin_enabled"] is True


# --- alert rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"threshold": 1.5},  # an error rate is a share, at most 0.999
        {"kind": "p95_latency", "threshold": 0},
        {"kind": "failure_rise", "threshold": 1.0},
        {"window_s": 90},  # metrics are per minute
        {"window_s": 7200},
        {"for_s": -1},
        {"name": "   "},
        {"service": "orders' OR 1=1 --"},
        {"kind": "cpu"},
    ],
)
async def test_invalid_rules_are_refused(admin_client: Any, change: dict[str, Any]) -> None:
    async with admin_client as c:
        r = await c.post("/api/alert-rules", json={**GOOD_RULE, **change}, headers=ADMIN)
    assert r.status_code == 422, r.text


async def test_create_update_delete_are_audited(admin_client: Any) -> None:
    async with admin_client as c:
        created = await c.post("/api/alert-rules", json=GOOD_RULE, headers=ADMIN)
        assert created.status_code == 201
        rule = created.json()
        assert rule["description"] == "error rate above 20% over 3 min, for 1 min"

        updated = await c.put(
            f"/api/alert-rules/{rule['id']}", json={**GOOD_RULE, "threshold": 0.3}, headers=ADMIN
        )
        assert updated.json()["threshold"] == 0.3
        assert (await c.delete(f"/api/alert-rules/{rule['id']}", headers=ADMIN)).status_code == 204
        assert (await c.delete(f"/api/alert-rules/{rule['id']}", headers=ADMIN)).status_code == 404
        log = (await c.get("/api/audit", headers=ADMIN)).json()["entries"]

    assert [e["action"] for e in log] == ["rule.delete", "rule.update", "rule.create"]
    assert all(e["actor"].startswith("admin@") for e in log)
    assert log[2]["details"]["service"] == "orders"


async def test_unavailable_rules_ignore_any_threshold(admin_client: Any) -> None:
    body = {**GOOD_RULE, "kind": "unavailable", "threshold": 42}
    async with admin_client as c:
        r = await c.post("/api/alert-rules", json=body, headers=ADMIN)
    assert r.status_code == 201 and r.json()["threshold"] == 0


async def open_incident(db_url: str, rule_id: int = 1, service: str = "gateway") -> int:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        incident_id = (
            await conn.execute(
                text(
                    "INSERT INTO incidents (rule_id, rule_name, kind, service, threshold, state, "
                    "opened_at, peak_value) VALUES (:r, 'Checkout errors', 'error_rate', :s, 0.1, "
                    "'open', now(), 0.5) RETURNING id"
                ),
                {"r": rule_id, "s": service},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO incident_events (incident_id, time, type, value, sample_trace_ids) "
                "VALUES (:i, now(), 'opened', 0.5, ARRAY['" + "ab" * 16 + "'])"
            ),
            {"i": incident_id},
        )
    await engine.dispose()
    return int(incident_id)


async def test_deleting_a_rule_resolves_its_incident_but_keeps_its_history(
    admin_client: Any, clean_db: str
) -> None:
    incident_id = await open_incident(clean_db)
    async with admin_client as c:
        assert (await c.delete("/api/alert-rules/1", headers=ADMIN)).status_code == 204
        incident = (await c.get(f"/api/incidents/{incident_id}")).json()
    assert incident["state"] == "resolved"
    assert incident["rule_id"] is None and incident["rule_name"] == "Checkout errors"
    assert incident["events"][-1]["detail"] == "the rule was deleted"


async def test_changing_what_a_rule_measures_closes_its_incident(
    admin_client: Any, clean_db: str
) -> None:
    incident_id = await open_incident(clean_db)
    async with admin_client as c:
        same_kind = {**GOOD_RULE, "service": "gateway", "threshold": 0.05}
        await c.put("/api/alert-rules/1", json=same_kind, headers=ADMIN)
        assert (await c.get(f"/api/incidents/{incident_id}")).json()["state"] == "open"
        other_kind = {**same_kind, "kind": "p95_latency", "threshold": 800}
        await c.put("/api/alert-rules/1", json=other_kind, headers=ADMIN)
        assert (await c.get(f"/api/incidents/{incident_id}")).json()["state"] == "resolved"


# --- incidents -----------------------------------------------------------------------


async def test_incident_list_detail_and_acknowledge(admin_client: Any, clean_db: str) -> None:
    incident_id = await open_incident(clean_db)
    async with admin_client as c:
        listing = (await c.get("/api/incidents", params={"state": "open"})).json()
        assert [i["id"] for i in listing["incidents"]] == [incident_id]
        assert listing["incidents"][0]["sample_trace_ids"] == ["ab" * 16]

        first = await c.post(f"/api/incidents/{incident_id}/ack", headers=ADMIN)
        again = await c.post(f"/api/incidents/{incident_id}/ack", headers=ADMIN)
        assert first.status_code == again.status_code == 200
        assert first.json()["acknowledged_at"] == again.json()["acknowledged_at"]
        types = [e["type"] for e in again.json()["events"]]
        assert types == ["opened", "acknowledged"]  # once, not twice

        assert (await c.get("/api/incidents/999999")).status_code == 404
        resolved = (await c.get("/api/incidents", params={"state": "resolved"})).json()
        assert resolved["incidents"] == []


async def test_a_resolved_incident_cannot_be_acknowledged(admin_client: Any, clean_db: str) -> None:
    incident_id = await open_incident(clean_db)
    async with admin_client as c:
        await c.delete("/api/alert-rules/1", headers=ADMIN)  # resolves it
        r = await c.post(f"/api/incidents/{incident_id}/ack", headers=ADMIN)
    assert r.status_code == 409


async def test_incident_history_pages_without_gaps(admin_client: Any, clean_db: str) -> None:
    ids = [await open_incident(clean_db, rule_id=1, service=f"svc{i}") for i in range(5)]
    seen: list[int] = []
    cursor = None
    async with admin_client as c:
        while True:
            params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/api/incidents", params=params)).json()
            seen += [i["id"] for i in page["incidents"]]
            if not (cursor := page["next_cursor"]):
                break
        bad = await c.get("/api/incidents", params={"cursor": "not-a-cursor"})
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))
    assert bad.status_code == 400


# --- settings --------------------------------------------------------------------------


async def test_retention_settings_round_trip_within_limits(admin_client: Any) -> None:
    async with admin_client as c:
        assert (await c.get("/api/settings/retention")).json()["spans"] == 7
        too_long = {"spans": 365, "logs": 3, "rollups": 10}
        assert (
            await c.put("/api/settings/retention", json=too_long, headers=ADMIN)
        ).status_code == 422
        ok = {"spans": 3, "logs": 2, "rollups": 10}
        saved = await c.put("/api/settings/retention", json=ok, headers=ADMIN)
        config = (await c.get("/api/config")).json()
        log = (await c.get("/api/audit", headers=ADMIN)).json()["entries"]
    assert saved.json() == {**ok, "incidents": 90}
    assert config["retention_days"]["spans"] == 3
    assert log[0]["action"] == "settings.retention"
    assert log[0]["details"]["before"]["spans"] == 7


# --- failure scenarios: the three locks ----------------------------------------------


def test_production_refuses_to_start_with_failure_injection(clean_db: str) -> None:
    with pytest.raises(ValidationError, match="only allowed with SERVSCOPE_ENV=demo"):
        Settings(
            database_url=clean_db,
            ingest_token=SecretStr("x" * 20),
            env="production",
            failure_injection=True,
            admin_token=SecretStr(ADMIN_TOKEN),
            fault_token=SecretStr(FAULT_TOKEN),
        )


def test_failure_injection_needs_both_tokens(clean_db: str) -> None:
    with pytest.raises(ValidationError, match="needs SERVSCOPE_ADMIN_TOKEN"):
        settings(clean_db, failure_injection=True, admin_token=SecretStr(ADMIN_TOKEN))


@pytest.mark.parametrize(
    "overrides",
    [
        {"env": "demo"},  # injection not switched on
        {"env": "production"},
        {"env": "production", "admin_token": SecretStr(ADMIN_TOKEN)},
    ],
)
async def test_scenario_routes_do_not_exist_unless_both_locks_are_open(
    make_client: Any, overrides: dict[str, Any]
) -> None:
    async with make_client(**overrides) as c:
        listing = await c.get("/api/scenarios")
        start = await c.post("/api/scenarios/payments_timeout/start", headers=ADMIN)
        stop = await c.post("/api/scenarios/stop", headers=ADMIN)
        config = (await c.get("/api/config")).json()
    assert listing.status_code == start.status_code == stop.status_code == 404
    assert config["scenarios_enabled"] is False


class DemoShop(httpx.AsyncBaseTransport):
    """Routes ServScope's calls to the real payments and orders fault switches."""

    def __init__(self) -> None:
        exporters = Exporters(InMemorySpanExporter(), InMemoryLogRecordExporter())
        self.faults = {"payments": Faults(), "orders": Faults()}
        self.apps = {
            "payments": payments.create_app(exporters, faults=self.faults["payments"]),
            "orders": orders.create_app(
                "http://inventory", "http://payments", exporters, faults=self.faults["orders"]
            ),
        }
        self.down: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((host, request.method))
        if host in self.down:
            raise httpx.ConnectError("connection refused", request=request)
        return await httpx.ASGITransport(app=self.apps[host]).handle_async_request(request)


@pytest.fixture
async def demo(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, DemoShop]]:
    monkeypatch.setenv("DEMO_FAULT_TOKEN", FAULT_TOKEN)  # the shop's side of the switch
    shop = DemoShop()
    client = make_client(
        fault_transport=shop,
        env="demo",
        failure_injection=True,
        admin_token=SecretStr(ADMIN_TOKEN),
        fault_token=SecretStr(FAULT_TOKEN),
        payments_url="http://payments",
        orders_url="http://orders",
    )
    async with client as c:
        yield c, shop


async def test_starting_a_scenario_needs_the_admin_token(demo: tuple[Any, DemoShop]) -> None:
    c, shop = demo
    for headers in ({}, WRONG):
        r = await c.post("/api/scenarios/payments_timeout/start", headers=headers)
        assert r.status_code == 401
    assert shop.calls == []  # refused before anything reached the shop
    assert shop.faults["payments"].current is None


async def test_a_scenario_flips_the_real_switch_and_is_audited(demo: tuple[Any, DemoShop]) -> None:
    c, shop = demo
    r = await c.post(
        "/api/scenarios/payments_timeout/start", json={"duration_s": 120}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    fault = shop.faults["payments"].current
    assert fault is not None and (fault.delay_ms, fault.duration_s) == (3000, 120)
    assert r.json()["active"]["name"] == "payments_timeout"
    assert 119 <= r.json()["active"]["remaining_s"] <= 120

    # Starting another one first switches the old one off: never two faults at once.
    await c.post("/api/scenarios/inventory_unreachable/start", headers=ADMIN)
    assert shop.faults["payments"].current is None
    assert shop.faults["orders"].current is not None
    assert (await c.get("/api/scenarios")).json()["active"]["name"] == "inventory_unreachable"

    stopped = await c.post("/api/scenarios/stop", headers=ADMIN)
    assert stopped.json()["active"] is None
    assert all(f.current is None for f in shop.faults.values())
    log = (await c.get("/api/audit", headers=ADMIN)).json()["entries"]
    assert [e["action"] for e in log] == ["scenario.stop", "scenario.start", "scenario.start"]


@pytest.mark.parametrize("duration", [0, 29, 601, 86_400])
async def test_scenarios_are_capped_at_ten_minutes(
    demo: tuple[Any, DemoShop], duration: int
) -> None:
    c, shop = demo
    r = await c.post(
        "/api/scenarios/payments_slow/start", json={"duration_s": duration}, headers=ADMIN
    )
    assert r.status_code == 422
    assert shop.faults["payments"].current is None


async def test_unknown_scenario_and_unreachable_shop(demo: tuple[Any, DemoShop]) -> None:
    c, shop = demo
    assert (await c.post("/api/scenarios/rm_rf/start", headers=ADMIN)).status_code == 404
    shop.down.add("payments")
    r = await c.post("/api/scenarios/payments_slow/start", headers=ADMIN)
    assert r.status_code == 502
    listing = (await c.get("/api/scenarios")).json()
    assert listing["targets"] == {"payments": "unreachable", "orders": "reachable"}
