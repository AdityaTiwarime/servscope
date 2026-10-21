"""Demo shop: orders. Reserves stock, then charges the card.

If the charge fails or times out, the reservation is released (a compensating action), so
a failed order never keeps stock locked away.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Status, StatusCode

from .schemas import Order, OrderRequest, Problem
from .telemetry import setup_tracing, traced_client

log = logging.getLogger("demo.orders")

PRICES = {"tea": 12_000, "mug": 34_900, "beans": 54_000}
DEFAULT_PRICE = 10_000
INVENTORY_TIMEOUT = 1.0
PAYMENT_TIMEOUT = 2.0  # the design's "payments timeout" scenario trips this


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(Problem(title=title, status=status, detail=detail).model_dump(), status)


def create_app(
    inventory_url: str,
    payments_url: str,
    exporter: SpanExporter | None = None,
    inventory_transport: httpx.AsyncBaseTransport | None = None,
    payments_transport: httpx.AsyncBaseTransport | None = None,
    payment_timeout: float = PAYMENT_TIMEOUT,
) -> FastAPI:
    app = FastAPI(title="Demo orders", docs_url=None, redoc_url=None)
    provider = setup_tracing(app, "orders", exporter)
    inventory = traced_client(provider, inventory_url, inventory_transport)
    payments = traced_client(provider, payments_url, payments_transport)

    async def release(reservation_id: str) -> None:
        try:
            async with asyncio.timeout(INVENTORY_TIMEOUT):
                await inventory.delete(f"/reservations/{reservation_id}")
        except (TimeoutError, httpx.HTTPError):
            # Logged, not raised: the customer already has their answer. A periodic sweep of
            # stale reservations would clean this up; it is out of scope for the demo.
            log.warning("could not release reservation %s", reservation_id)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/orders", status_code=201, response_model=Order)
    async def place_order(body: OrderRequest) -> Order | JSONResponse:
        span = trace.get_current_span()
        order_id = uuid.uuid4().hex[:16]
        total = sum(PRICES.get(i.sku, DEFAULT_PRICE) * i.qty for i in body.items)
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.total_paise", total)
        items = [i.model_dump() for i in body.items]

        # 1. Reserve stock.
        try:
            async with asyncio.timeout(INVENTORY_TIMEOUT):
                r = await inventory.post(
                    "/reservations", json={"order_id": order_id, "items": items}
                )
        except (TimeoutError, httpx.HTTPError) as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", "inventory_unavailable")
            return _problem(503, "Inventory unavailable")
        if r.status_code == 409:
            return _problem(409, "Out of stock", r.json().get("detail", ""))
        if r.status_code != 201:
            return _problem(502, "Inventory error", f"inventory answered {r.status_code}")
        reservation_id = r.json()["reservation_id"]

        # 2. Charge the card. The order ID is the idempotency key, so a retry of this exact
        #    call can never charge the customer twice.
        charge = {"order_id": order_id, "amount_paise": total, "card_token": body.card_token}
        try:
            async with asyncio.timeout(payment_timeout):
                p = await payments.post(
                    "/charges", json=charge, headers={"Idempotency-Key": order_id}
                )
        except TimeoutError as exc:
            # error.type is the OpenTelemetry convention for "why did this fail"; the span
            # status itself is set to ERROR by the instrumentation because we answer 504.
            span.record_exception(exc)
            span.set_attribute("error.type", "payments_timeout")
            span.set_status(
                Status(StatusCode.ERROR, f"payments timed out after {payment_timeout}s")
            )
            await release(reservation_id)
            return _problem(504, "Payment timed out")
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", "payments_unavailable")
            await release(reservation_id)
            return _problem(503, "Payments unavailable")

        if p.status_code == 402:
            await release(reservation_id)
            return _problem(402, "Card declined")
        if p.status_code not in (200, 201):
            await release(reservation_id)
            return _problem(502, "Payments error", f"payments answered {p.status_code}")

        return Order(order_id=order_id, total_paise=total, charge_id=p.json()["charge_id"])

    return app


def main() -> None:
    app = create_app(
        inventory_url=os.environ.get("INVENTORY_URL", "http://localhost:8082"),
        payments_url=os.environ.get("PAYMENTS_URL", "http://localhost:8083"),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8081")))  # noqa: S104


if __name__ == "__main__":
    main()
