"""Demo shop: orders. Reserves stock, then charges the card.

If the charge fails or times out, the reservation is released (a compensating action), so
a failed order never keeps stock locked away. If the charge timed out, orders also voids it
first: the customer was told the order failed, so no money may be taken for it.
"""

from __future__ import annotations

import os
import uuid

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .faults import Faults, install
from .schemas import Order, OrderRequest, Problem
from .telemetry import Downstream, Exporters, setup_telemetry

PRICES = {"tea": 12_000, "mug": 34_900, "beans": 54_000}
DEFAULT_PRICE = 10_000
INVENTORY_TIMEOUT = 1.0
PAYMENT_TIMEOUT = 2.0  # the design's "payments timeout" scenario trips this
VOID_TIMEOUT = 1.0


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(Problem(title=title, status=status, detail=detail).model_dump(), status)


def create_app(
    inventory_url: str,
    payments_url: str,
    exporters: Exporters | None = None,
    inventory_transport: httpx.AsyncBaseTransport | None = None,
    payments_transport: httpx.AsyncBaseTransport | None = None,
    payment_timeout: float = PAYMENT_TIMEOUT,
    faults: Faults | None = None,
) -> FastAPI:
    app = FastAPI(title="Demo orders", docs_url=None, redoc_url=None)
    provider, log = setup_telemetry(app, "orders", exporters)
    faults = faults or Faults()
    install(app, faults, os.environ.get("DEMO_FAULT_TOKEN"), log)
    # The "inventory unreachable" scenario cuts orders off from inventory at the network level.
    cut = faults.is_cut_off
    inventory = Downstream(provider, "inventory", inventory_url, inventory_transport, cut)
    payments = Downstream(provider, "payments", payments_url, payments_transport, cut)

    async def void_charge(order_id: str) -> None:
        """Cancel the charge for an order the customer was told failed. Payments refuses a
        charge that arrives after this, so the order of the two calls does not matter."""
        try:
            v = await payments.request(
                "POST",
                "/charges/void",
                deadline_s=VOID_TIMEOUT,
                headers={"Idempotency-Key": order_id},
            )
        except (TimeoutError, httpx.HTTPError):
            # The one case left open: neither the charge nor the void got an answer. A real
            # system would retry the void from a queue until it succeeds; noted in the README.
            log.error("could not void the charge for order %s; it may need a refund", order_id)
            return
        if v.status_code == 200 and v.json().get("voided"):
            log.warning("order %s: payments had charged after we gave up; charge voided", order_id)

    async def release(reservation_id: str) -> None:
        try:
            await inventory.request(
                "DELETE", f"/reservations/{reservation_id}", deadline_s=INVENTORY_TIMEOUT
            )
        except (TimeoutError, httpx.HTTPError):
            # Logged, not raised: the customer already has their answer. A periodic sweep of
            # stale reservations would clean this up; it is out of scope for the demo.
            log.error("could not release reservation %s; stock stays held", reservation_id)
        else:
            log.info("released reservation %s", reservation_id)

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
            r = await inventory.request(
                "POST",
                "/reservations",
                deadline_s=INVENTORY_TIMEOUT,
                json={"order_id": order_id, "items": items},
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", "inventory_unavailable")
            log.error("order %s failed: inventory unavailable (%s)", order_id, type(exc).__name__)
            return _problem(503, "Inventory unavailable")
        if r.status_code == 409:
            log.warning("order %s rejected: out of stock (%s)", order_id, r.json().get("detail"))
            return _problem(409, "Out of stock", r.json().get("detail", ""))
        if r.status_code != 201:
            return _problem(502, "Inventory error", f"inventory answered {r.status_code}")
        reservation_id = r.json()["reservation_id"]

        # 2. Charge the card. The order ID is the idempotency key, so a retry of this exact
        #    call can never charge the customer twice.
        charge = {"order_id": order_id, "amount_paise": total, "card_token": body.card_token}
        try:
            p = await payments.request(
                "POST",
                "/charges",
                deadline_s=payment_timeout,
                json=charge,
                headers={"Idempotency-Key": order_id},
            )
        except TimeoutError as exc:
            # error.type is the OpenTelemetry convention for "why did this fail"; the span
            # status itself is set to ERROR by the instrumentation because we answer 504.
            span.record_exception(exc)
            span.set_attribute("error.type", "payments_timeout")
            span.set_status(
                Status(StatusCode.ERROR, f"payments timed out after {payment_timeout}s")
            )
            log.error(
                "order %s failed: payments timed out after %ss, voiding the charge "
                "and releasing stock",
                order_id,
                payment_timeout,
            )
            await void_charge(order_id)
            await release(reservation_id)
            return _problem(504, "Payment timed out")
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", "payments_unavailable")
            log.error("order %s failed: payments unavailable (%s)", order_id, type(exc).__name__)
            # The connection may have dropped after payments got the request, so the charge
            # may exist. A void is harmless when it doesn't.
            await void_charge(order_id)
            await release(reservation_id)
            return _problem(503, "Payments unavailable")

        if p.status_code == 402:
            log.warning("order %s rejected: card declined, releasing stock", order_id)
            await release(reservation_id)
            return _problem(402, "Card declined")
        if p.status_code not in (200, 201):
            await release(reservation_id)
            return _problem(502, "Payments error", f"payments answered {p.status_code}")

        log.info("order %s placed, total %s paise", order_id, total)
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
