"""Demo shop: payments. A fake card processor with idempotent charges.

The Idempotency-Key header makes a retried charge safe: the same key always returns the
same charge, and the customer is charged once.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from .schemas import Charge, ChargeRequest, Problem
from .telemetry import Exporters, setup_telemetry

DECLINED_TOKEN = "tok_demo_declined"  # noqa: S105  # a demo constant, not a secret


def create_app(exporters: Exporters | None = None) -> FastAPI:
    app = FastAPI(title="Demo payments", docs_url=None, redoc_url=None)
    provider, log = setup_telemetry(app, "payments", exporters)
    tracer = provider.get_tracer("demo.payments")

    charges: dict[str, Charge] = {}  # idempotency key -> charge
    lock = asyncio.Lock()
    # Extra latency before answering. Tests set it directly; from Milestone 5 the guarded
    # failure-scenario switch sets it. There is deliberately no HTTP endpoint for it here.
    app.state.delay_seconds = 0.0
    app.state.charge_count = 0

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/charges", status_code=201, response_model=Charge)
    async def charge(
        body: ChargeRequest, idempotency_key: str = Header(min_length=8, max_length=64)
    ) -> Charge | JSONResponse:
        if app.state.delay_seconds:
            await asyncio.sleep(app.state.delay_seconds)

        async with lock:  # two retries racing with one key must still charge once
            if idempotency_key in charges:
                done = charges[idempotency_key]
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
                    # The order and amount, never the card token.
                    log.warning("card declined for order %s", body.order_id)
                    problem = Problem(title="Card declined", status=402)
                    return JSONResponse(problem.model_dump(), status_code=402)
                result = Charge(
                    charge_id=f"ch_{uuid.uuid4().hex[:12]}",
                    order_id=body.order_id,
                    amount_paise=body.amount_paise,
                )
                charges[idempotency_key] = result
                app.state.charge_count += 1
                log.info("charged %s paise for order %s", body.amount_paise, body.order_id)
        return result

    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8083")))  # noqa: S104


if __name__ == "__main__":
    main()
