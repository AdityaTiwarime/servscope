"""Demo shop: the gateway, the entry point for every customer request.

It validates the request and forwards it to orders. Errors from downstream keep their
meaning (409 out of stock, 402 declined); anything unexpected becomes a 502 or 504.
"""

from __future__ import annotations

import os

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .schemas import Checkout, Order, Problem
from .telemetry import Downstream, Exporters, setup_telemetry

# Longer than orders' own worst case (1 s inventory + 2 s payments + 1 s release), so the
# gateway hears orders' specific error instead of timing out first.
ORDERS_TIMEOUT = 5.0
PASSED_THROUGH = {402, 409, 503, 504}


def create_app(
    orders_url: str,
    exporters: Exporters | None = None,
    orders_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(title="Demo gateway", docs_url=None, redoc_url=None)
    provider, log = setup_telemetry(app, "gateway", exporters)
    orders = Downstream(provider, "orders", orders_url, orders_transport)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/checkout", status_code=201, response_model=Order)
    async def checkout(body: Checkout) -> Order | JSONResponse:
        try:
            r = await orders.request(
                "POST", "/orders", deadline_s=ORDERS_TIMEOUT, json=body.model_dump()
            )
        except TimeoutError:
            log.error("checkout failed: orders did not answer within %ss", ORDERS_TIMEOUT)
            return JSONResponse(Problem(title="Orders timed out", status=504).model_dump(), 504)
        except httpx.HTTPError as exc:
            log.error("checkout failed: orders unreachable (%s)", type(exc).__name__)
            return JSONResponse(Problem(title="Orders unavailable", status=503).model_dump(), 503)

        if r.status_code == 201:
            return Order.model_validate(r.json())
        if r.status_code in PASSED_THROUGH:
            log.warning("checkout not completed: orders answered %s", r.status_code)
            return JSONResponse(r.json(), status_code=r.status_code)
        problem = Problem(
            title="Order failed", status=502, detail=f"orders answered {r.status_code}"
        )
        return JSONResponse(problem.model_dump(), 502)

    return app


def main() -> None:
    app = create_app(orders_url=os.environ.get("ORDERS_URL", "http://localhost:8081"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104


if __name__ == "__main__":
    main()
