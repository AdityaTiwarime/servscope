"""Demo shop: the gateway, the entry point for every customer request.

In Milestone 1 it prices the cart itself; Milestone 2 replaces that with a call to the
orders service, which then calls inventory and payments.
"""

from __future__ import annotations

import os
import uuid

import uvicorn
from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel, Field

from .telemetry import setup_tracing

# Fixed demo catalogue, prices in paise so no float rounding ever touches money.
PRICES = {"tea": 12_000, "mug": 34_900, "beans": 54_000}


class Item(BaseModel):
    sku: str = Field(pattern=r"^[a-z]{1,32}$")
    qty: int = Field(ge=1, le=20)


class Checkout(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=20)


class OrderPlaced(BaseModel):
    order_id: str
    total_paise: int


app = FastAPI(title="Demo gateway", docs_url=None, redoc_url=None)
setup_tracing(app, "gateway")
tracer = trace.get_tracer("demo.gateway")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/checkout", status_code=201)
async def checkout(body: Checkout) -> OrderPlaced:
    with tracer.start_as_current_span("price_cart") as span:
        total = sum(PRICES.get(i.sku, 10_000) * i.qty for i in body.items)
        span.set_attribute("cart.items", len(body.items))
        span.set_attribute("cart.total_paise", total)
    return OrderPlaced(order_id=uuid.uuid4().hex[:12], total_paise=total)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104


if __name__ == "__main__":
    main()
