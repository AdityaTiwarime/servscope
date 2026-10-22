"""Demo shop: payments. A fake card processor with idempotent charges.

The Idempotency-Key header makes a retried charge safe: the same key always returns the
same charge, and the customer is charged once.

How a timed-out order is kept from charging the card (the Milestone 4 double-charge bug):

1. Deadline. The caller sends how long it will wait (X-Request-Timeout-Ms). Payments stops
   working on the charge when that budget runs out and answers 504 without charging,
   because nobody is waiting for the answer any more.
2. Void. The budget is measured from when payments received the call, a few milliseconds
   after the caller started its own timer, so a charge could still complete inside that gap.
   So when the caller gives up it also sends POST /charges/void with the same key. A void
   for a completed charge reverses it; a void that arrives first leaves a marker, and a
   charge that turns up later for that key is refused. Either order ends with no money taken.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from opentelemetry import trace

from .faults import Faults, install
from .schemas import Charge, ChargeRequest, Problem, VoidResult
from .telemetry import Exporters, setup_telemetry

DECLINED_TOKEN = "tok_demo_declined"  # noqa: S105  # a demo constant, not a secret

IdempotencyKey = Annotated[str, Header(min_length=8, max_length=64)]
Budget = Annotated[int | None, Header(alias="X-Request-Timeout-Ms", ge=1, le=60_000)]


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(Problem(title=title, status=status, detail=detail).model_dump(), status)


def create_app(exporters: Exporters | None = None, faults: Faults | None = None) -> FastAPI:
    app = FastAPI(title="Demo payments", docs_url=None, redoc_url=None)
    provider, log = setup_telemetry(app, "payments", exporters)
    tracer = provider.get_tracer("demo.payments")
    faults = faults or Faults()
    install(app, faults, os.environ.get("DEMO_FAULT_TOKEN"), log)

    charges: dict[str, Charge] = {}  # idempotency key -> charge
    voided: set[str] = set()  # keys whose order was cancelled; later charges are refused
    lock = asyncio.Lock()
    app.state.charge_count = 0  # charges captured
    app.state.void_count = 0  # captured charges reversed afterwards
    app.state.abandoned_count = 0  # charges stopped because the caller's deadline passed

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def card_network(fault_delay_s: float) -> None:
        """Stands in for the call to the card network; a demo fault can make it slow."""
        if fault_delay_s:
            await asyncio.sleep(fault_delay_s)

    @app.post("/charges", status_code=201, response_model=Charge)
    async def charge(
        body: ChargeRequest, idempotency_key: IdempotencyKey, budget_ms: Budget = None
    ) -> Charge | JSONResponse:
        span = trace.get_current_span()
        fault = faults.current
        if fault and fault.error_rate and random.random() < fault.error_rate:  # noqa: S311
            span.set_attribute("demo.fault", "error")
            log.error("charge for order %s failed: card network unavailable", body.order_id)
            return _problem(503, "Card network unavailable")

        try:
            # No budget sent (an older caller): work as long as it takes, as before.
            async with asyncio.timeout(budget_ms / 1000 if budget_ms else None):
                await card_network(fault.delay_ms / 1000 if fault else 0.0)
                async with lock:  # two retries racing with one key must still charge once
                    return await capture(body, idempotency_key)
        except TimeoutError:
            app.state.abandoned_count += 1
            span.set_attribute("payment.abandoned", "deadline_exceeded")
            log.warning(
                "charge for order %s abandoned: the caller stopped waiting after %s ms",
                body.order_id,
                budget_ms,
            )
            return _problem(504, "Deadline exceeded", "the caller is no longer waiting")

    async def capture(body: ChargeRequest, key: str) -> Charge | JSONResponse:
        if key in voided:
            log.warning(
                "charge for order %s refused: the order was already cancelled", body.order_id
            )
            return _problem(409, "Order cancelled", "this charge was voided before it arrived")
        if key in charges:
            done = charges[key]
            log.info("repeat charge for order %s answered from the first one", body.order_id)
            return JSONResponse(
                done.model_dump(), status_code=200, headers={"Idempotent-Replay": "true"}
            )
        with tracer.start_as_current_span("authorize_card") as span:
            # Attributes describe the charge, never the card token.
            span.set_attribute("order.id", body.order_id)
            span.set_attribute("payment.amount_paise", body.amount_paise)
            if body.card_token == DECLINED_TOKEN:
                span.set_attribute("payment.declined", True)
                log.warning("card declined for order %s", body.order_id)
                return _problem(402, "Card declined")
            result = Charge(
                charge_id=f"ch_{uuid.uuid4().hex[:12]}",
                order_id=body.order_id,
                amount_paise=body.amount_paise,
            )
            charges[key] = result
            app.state.charge_count += 1
            log.info("charged %s paise for order %s", body.amount_paise, body.order_id)
        return result

    @app.post("/charges/void", response_model=VoidResult)
    async def void(idempotency_key: IdempotencyKey) -> VoidResult:
        """Cancel whatever happened, or will happen, under this key. Safe to repeat."""
        span = trace.get_current_span()
        async with lock:
            first_void = idempotency_key not in voided
            voided.add(idempotency_key)
            done = charges.get(idempotency_key)
            if done is not None and done.status == "captured":
                charges[idempotency_key] = done.model_copy(update={"status": "voided"})
                app.state.void_count += 1
                span.set_attribute("payment.voided", True)
                log.warning("voided charge %s for order %s", done.charge_id, done.order_id)
                return VoidResult(voided=True, charge_id=done.charge_id)
        if done is None and first_void:
            log.info("void recorded before any charge; a late charge with this key is refused")
        return VoidResult(voided=False, charge_id=done.charge_id if done else None)

    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8083")))  # noqa: S104


if __name__ == "__main__":
    main()
