"""Demo shop: inventory. Reserves stock for an order, all items or none."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from .schemas import Problem, Reservation, ReservationRequest
from .telemetry import Exporters, setup_telemetry

INITIAL_STOCK = {"tea": 100, "mug": 50, "beans": 30}


def create_app(exporters: Exporters | None = None, restock_every_s: float = 0) -> FastAPI:
    """`restock_every_s` > 0 tops stock back up on that schedule (a supplier delivery), so
    the long-running demo with steady traffic doesn't end up permanently out of stock."""
    stock = dict(INITIAL_STOCK)
    reservations: dict[str, dict[str, int]] = {}
    # One lock around check-and-take, so two orders can't both grab the last mug.
    lock = asyncio.Lock()

    async def restock_forever() -> None:
        while True:
            await asyncio.sleep(restock_every_s)
            async with lock:
                low = {sku: n for sku, n in stock.items() if n < INITIAL_STOCK[sku]}
                stock.update({sku: INITIAL_STOCK[sku] for sku in low})
            if low:
                log.info("restocked %s", sorted(low))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(restock_forever()) if restock_every_s > 0 else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Demo inventory", docs_url=None, redoc_url=None, lifespan=lifespan)
    provider, log = setup_telemetry(app, "inventory", exporters)
    tracer = provider.get_tracer("demo.inventory")
    app.state.stock = stock

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/reservations", status_code=201, response_model=Reservation)
    async def reserve(body: ReservationRequest) -> Reservation | JSONResponse:
        wanted: dict[str, int] = {}
        for item in body.items:
            wanted[item.sku] = wanted.get(item.sku, 0) + item.qty

        with tracer.start_as_current_span("reserve_stock") as span:
            span.set_attribute("order.id", body.order_id)
            async with lock:
                short = [sku for sku, qty in wanted.items() if stock.get(sku, 0) < qty]
                if short:
                    span.set_attribute("inventory.short_skus", short)
                    log.warning("cannot reserve for order %s: short of %s", body.order_id, short)
                    problem = Problem(title="Out of stock", status=409, detail=", ".join(short))
                    return JSONResponse(problem.model_dump(), status_code=409)
                for sku, qty in wanted.items():
                    stock[sku] -= qty
                rid = uuid.uuid4().hex[:12]
                reservations[rid] = wanted
        log.info("reserved %s for order %s as %s", wanted, body.order_id, rid)
        return Reservation(reservation_id=rid)

    @app.delete("/reservations/{rid}", status_code=204)
    async def release(rid: str) -> Response:
        # Idempotent: releasing twice, or releasing an unknown ID, is not an error, because
        # orders may retry a release after a network hiccup.
        async with lock:
            for sku, qty in reservations.pop(rid, {}).items():
                stock[sku] += qty
        return Response(status_code=204)

    @app.get("/stock")
    async def get_stock() -> dict[str, int]:
        return dict(stock)

    return app


def main() -> None:
    app = create_app(restock_every_s=float(os.environ.get("RESTOCK_EVERY_S", "0")))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8082")))  # noqa: S104


if __name__ == "__main__":
    main()
