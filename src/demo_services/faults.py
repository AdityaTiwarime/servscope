"""The fault switch that ServScope's Demo scenarios flip.

Each demo service that can misbehave on purpose keeps one `Faults` object. ServScope sets it
through `PUT /_faults`, and the service's own code reads it on every request.

Safety, on the demo side (ServScope adds its own locks on top):

- The endpoint exists only when the service is started with DEMO_FAULT_TOKEN set, and every
  call must carry that token. Without the variable there is nothing to call.
- Every fault has a duration (at most 10 minutes) and switches itself off when it runs out,
  measured on this service's own clock. If ServScope crashes mid-scenario, the shop still
  recovers on its own.
- Switching a fault on or off is written to the service's log, so the incident timeline in
  ServScope can be checked against what the service says happened.
- /_faults is left out of tracing, so flipping the switch never shows up as shop traffic.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

MAX_DURATION_S = 600
# Module level on purpose: with `from __future__ import annotations` FastAPI resolves
# annotations from module globals, and a local alias would silently never read the header.
TokenHeader = Annotated[str | None, Header(alias="X-Fault-Token")]


class FaultSpec(BaseModel):
    delay_ms: int = Field(default=0, ge=0, le=5000)  # extra latency before answering
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # share of calls answered with 503
    unreachable: list[Literal["inventory", "payments"]] = Field(default_factory=list, max_length=2)
    duration_s: int = Field(ge=1, le=MAX_DURATION_S)


class FaultState(BaseModel):
    active: FaultSpec | None
    remaining_s: float


class Faults:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._spec: FaultSpec | None = None
        self._until = 0.0

    def set(self, spec: FaultSpec) -> None:
        self._spec, self._until = spec, self._clock() + spec.duration_s

    def clear(self) -> None:
        self._spec = None

    @property
    def current(self) -> FaultSpec | None:
        if self._spec is not None and self._clock() >= self._until:
            self._spec = None  # expired: the fault switches itself off
        return self._spec

    def state(self) -> FaultState:
        spec = self.current
        remaining = max(0.0, self._until - self._clock()) if spec else 0.0
        return FaultState(active=spec, remaining_s=round(remaining, 1))

    def is_cut_off(self, service: str) -> bool:
        spec = self.current
        return spec is not None and service in spec.unreachable


def install(app: FastAPI, faults: Faults, token: str | None, log: logging.Logger) -> None:
    """Adds GET/PUT/DELETE /_faults, guarded by `token`. No token: no endpoint at all."""
    app.state.faults = faults
    if not token:
        return

    def check(given: str | None) -> None:
        # compare_digest takes the same time however much of the token matches.
        if given is None or not secrets.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="wrong or missing fault token")

    @app.get("/_faults", include_in_schema=False)
    async def get_faults(x_fault_token: TokenHeader = None) -> FaultState:
        check(x_fault_token)
        return faults.state()

    @app.put("/_faults", include_in_schema=False)
    async def put_faults(spec: FaultSpec, x_fault_token: TokenHeader = None) -> FaultState:
        check(x_fault_token)
        faults.set(spec)
        log.warning(
            "demo fault switched ON for %ss: %s",
            spec.duration_s,
            spec.model_dump(exclude={"duration_s"}, exclude_defaults=True),
        )
        return faults.state()

    @app.delete("/_faults", status_code=204, include_in_schema=False)
    async def delete_faults(x_fault_token: TokenHeader = None) -> Response:
        check(x_fault_token)
        if faults.current is not None:
            log.warning("demo fault switched OFF")
        faults.clear()
        return Response(status_code=204)
