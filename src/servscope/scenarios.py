"""Demo scenarios: predefined failures switched on in the demo shop, safely.

Three locks, all required:

1. SERVSCOPE_ENV=demo, and
2. SERVSCOPE_FAILURE_INJECTION=true. Without both, these routes are never registered: there
   is nothing to call, so /api/scenarios answers 404. With env=production and injection on,
   ServScope refuses to start at all (config.py).
3. The admin token, on every start and stop.

On top of that, every scenario has a duration of at most 10 minutes, enforced by the demo
service's own clock (demo_services/faults.py), so a scenario ends even if ServScope dies.
Starting one stops any other first, so two faults never mix. Each start and stop is written
to the audit log.

ServScope reaches the shop's fault switches over a separate network in Docker Compose, with
their own token (SERVSCOPE_FAULT_TOKEN); the shop itself can still reach ServScope only
through the Collector.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Path, Request
from pydantic import BaseModel, Field

from .admin import Admin, audit
from .config import Settings
from .query import ApiError

MAX_DURATION_S = 600
SWITCH_TIMEOUT = httpx.Timeout(2.0)


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    target: str  # which demo service's switch is flipped
    fault: dict[str, Any]
    what_happens: str
    what_to_look_for: str


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "payments_slow",
            "Slow payments",
            "payments",
            {"delay_ms": 1500},
            "Every card charge takes 1.5 s longer. Checkouts still succeed.",
            "Gateway p95 rises, the waterfall shows the long payments span, and "
            '"Checkout latency (p95)" fires.',
        ),
        Scenario(
            "payments_timeout",
            "Payments timeout",
            "payments",
            {"delay_ms": 3000},
            "Card charges take 3 s, past orders' 2 s timeout, so checkouts fail with 504. "
            "Payments stops at the deadline and no card is charged.",
            '"Checkout errors" fires; the incident links failing traces; the orders span '
            "says payments timed out; its logs are one click away.",
        ),
        Scenario(
            "payments_errors",
            "Payments returns errors",
            "payments",
            {"error_rate": 0.3},
            "About 30% of card charges answer 503, so those checkouts fail.",
            "The orders → payments edge turns red, gateway error rate sits near 30%, and "
            '"Checkout errors" fires.',
        ),
        Scenario(
            "inventory_unreachable",
            "Inventory unreachable",
            "orders",
            {"unreachable": ["inventory"]},
            "Orders can no longer connect to inventory (connections are refused), so every "
            "checkout fails fast with 503.",
            'Inventory goes quiet: no data, then "Service unavailable" fires, with orders\' '
            "failed calls to inventory as its evidence.",
        ),
    )
}


class StartBody(BaseModel):
    duration_s: int = Field(default=300, ge=30, le=MAX_DURATION_S)


class FaultSwitches:
    """Talks to the demo services' /_faults endpoints."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        assert settings.fault_token is not None  # guaranteed by Settings validation
        self.urls = {"payments": settings.payments_url, "orders": settings.orders_url}
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=SWITCH_TIMEOUT,
            headers={"X-Fault-Token": settings.fault_token.get_secret_value()},
        )

    async def _call(self, target: str, method: str, **kwargs: Any) -> httpx.Response:
        r = await self.client.request(method, f"{self.urls[target]}/_faults", **kwargs)
        r.raise_for_status()
        return r

    async def state(self, target: str) -> dict[str, Any] | None:
        """The target's current fault state, or None when it can't be reached."""
        try:
            data: dict[str, Any] = (await self._call(target, "GET")).json()
            return data
        except httpx.HTTPError:
            return None

    async def set(self, target: str, fault: dict[str, Any], duration_s: int) -> None:
        await self._call(target, "PUT", json={**fault, "duration_s": duration_s})

    async def clear_all(self) -> dict[str, bool]:
        async def clear(target: str) -> bool:
            try:
                await self._call(target, "DELETE")
                return True
            except httpx.HTTPError:
                return False

        results = await asyncio.gather(*(clear(t) for t in self.urls))
        return dict(zip(self.urls, results, strict=True))

    async def aclose(self) -> None:
        await self.client.aclose()


def _matches(scenario: Scenario, active: dict[str, Any]) -> bool:
    return all(active.get(k) == v for k, v in scenario.fault.items())


def make_router(switches: FaultSwitches) -> APIRouter:
    router = APIRouter(prefix="/api/scenarios")

    async def status() -> dict[str, Any]:
        states = dict(
            zip(
                switches.urls,
                await asyncio.gather(*(switches.state(t) for t in switches.urls)),
                strict=True,
            )
        )
        active = None
        for target, state in states.items():
            if state and state.get("active"):
                name = next(
                    (s.name for s in SCENARIOS.values()
                     if s.target == target and _matches(s, state["active"])),
                    "custom",
                )  # fmt: skip
                active = {"name": name, "target": target, "remaining_s": state["remaining_s"]}
        return {
            "scenarios": [
                {
                    "name": s.name,
                    "title": s.title,
                    "target": s.target,
                    "fault": s.fault,
                    "what_happens": s.what_happens,
                    "what_to_look_for": s.what_to_look_for,
                }
                for s in SCENARIOS.values()
            ],
            "max_duration_s": MAX_DURATION_S,
            "active": active,
            "targets": {
                t: "reachable" if s is not None else "unreachable" for t, s in states.items()
            },
        }

    @router.get("")
    async def list_scenarios() -> dict[str, Any]:
        return await status()

    @router.post("/{name}/start")
    async def start(
        request: Request,
        name: Annotated[str, Path(max_length=64)],
        actor: Admin,
        body: Annotated[StartBody | None, Body()] = None,
    ) -> dict[str, Any]:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise ApiError(404, "No such scenario", f"choose one of: {', '.join(SCENARIOS)}")
        duration = (body or StartBody()).duration_s
        await switches.clear_all()  # one scenario at a time
        try:
            await switches.set(scenario.target, scenario.fault, duration)
        except httpx.HTTPError as exc:
            raise ApiError(
                502, "Demo service unreachable", f"could not reach {scenario.target}'s switch"
            ) from exc
        async with request.app.state.engine.begin() as conn:
            await audit(conn, actor, "scenario.start", name=name, duration_s=duration)
        return await status()

    @router.post("/stop")
    async def stop(request: Request, actor: Admin) -> dict[str, Any]:
        cleared = await switches.clear_all()
        async with request.app.state.engine.begin() as conn:
            await audit(conn, actor, "scenario.stop", cleared=cleared)
        if not any(cleared.values()):
            raise ApiError(502, "Demo services unreachable", "no fault switch answered")
        return {**await status(), "cleared": cleared}

    return router
