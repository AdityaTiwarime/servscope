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
from opentelemetry.sdk.trace.export import SpanExporter

from .schemas import Charge, ChargeRequest, Problem
from .telemetry import setup_tracing

DECLINED_TOKEN = "tok_demo_declined"  # noqa: S105  # a demo constant, not a secret


def create_app(exporter: SpanExporter | None = None) -> FastAPI:
    app = FastAPI(title="Demo payments", docs_url=None, redoc_url=None)
    provider = setup_tracing(app, "payments", exporter)
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
                return JSONResponse(
                    done.model_dump(), status_code=200, headers={"Idempotent-Replay": "true"}
                )
            with tracer.start_as_current_span("authorize_card") as span:
                # Attributes describe the charge, never the card token.
                span.set_attribute("order.id", body.order_id)
                span.set_attribute("payment.amount_paise", body.amount_paise)
                if body.card_token == DECLINED_TOKEN:
                    span.set_attribute("payment.declined", True)
                    problem = Problem(title="Card declined", status=402)
                    return JSONResponse(problem.model_dump(), status_code=402)
                result = Charge(
                    charge_id=f"ch_{uuid.uuid4().hex[:12]}",
                    order_id=body.order_id,
                    amount_paise=body.amount_paise,
                )
                charges[idempotency_key] = result
                app.state.charge_count += 1
        return result

    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8083")))  # noqa: S104


if __name__ == "__main__":
    main()
