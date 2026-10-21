"""The four demo services wired together in one process.

Every service has its own tracer and they talk over real HTTP semantics (httpx in-process
transports), so these tests check the trace exactly as the services produce it: one trace,
four services, and every parent-child link where it should be.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from demo_services import gateway, inventory, orders, payments
from demo_services.inventory import INITIAL_STOCK


class IsolatedASGITransport(httpx.ASGITransport):
    """An in-process transport that behaves like a network hop for tracing.

    A plain ASGITransport runs the called app in the caller's context, so the called service
    would "see" the caller's span without any header, which would hide a broken propagation.
    Running each call in an empty OpenTelemetry context means the `traceparent` header is the
    only way trace context can cross, exactly as between real processes."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        token = otel_context.attach(otel_context.Context())
        try:
            return await super().handle_async_request(request)
        finally:
            otel_context.detach(token)


@dataclass
class Shop:
    client: httpx.AsyncClient
    spans: InMemorySpanExporter
    inventory: Any
    payments: Any


async def build_shop(
    payment_timeout: float = 2.0, inventory_transport: httpx.AsyncBaseTransport | None = None
) -> Shop:
    spans = InMemorySpanExporter()
    inv = inventory.create_app(spans)
    pay = payments.create_app(spans)
    orders_app = orders.create_app(
        "http://inventory",
        "http://payments",
        spans,
        inventory_transport=inventory_transport or IsolatedASGITransport(app=inv),
        payments_transport=IsolatedASGITransport(app=pay),
        payment_timeout=payment_timeout,
    )
    gw = gateway.create_app("http://orders", spans, IsolatedASGITransport(app=orders_app))
    client = httpx.AsyncClient(transport=IsolatedASGITransport(app=gw), base_url="http://gateway")
    return Shop(client, spans, inv, pay)


@pytest.fixture
async def shop() -> AsyncIterator[Shop]:
    s = await build_shop()
    yield s
    await s.client.aclose()


def caller_headers() -> tuple[dict[str, str], str, str]:
    trace_id, span_id = os.urandom(16).hex(), os.urandom(8).hex()
    return {"traceparent": f"00-{trace_id}-{span_id}-01"}, trace_id, span_id


def hexid(n: int, width: int) -> str:
    return format(n, f"0{width}x")


def server_span(spans: list[ReadableSpan], service: str) -> ReadableSpan:
    [s] = [
        s
        for s in spans
        if s.kind == SpanKind.SERVER and s.resource.attributes["service.name"] == service
    ]
    return s


def parent_of(span: ReadableSpan, spans: list[ReadableSpan]) -> ReadableSpan:
    assert span.parent is not None
    [p] = [s for s in spans if s.context.span_id == span.parent.span_id]
    return p


TEA_AND_MUG = {"items": [{"sku": "tea", "qty": 2}, {"sku": "mug", "qty": 1}]}


async def test_one_checkout_is_one_trace_across_four_services(shop: Shop) -> None:
    headers, trace_id, caller_span = caller_headers()
    r = await shop.client.post("/checkout", json=TEA_AND_MUG, headers=headers)

    assert r.status_code == 201
    assert r.json()["total_paise"] == 2 * 12_000 + 34_900
    spans = list(shop.spans.get_finished_spans())

    # Every span belongs to the caller's trace, and every parent exists in it.
    assert {hexid(s.context.trace_id, 32) for s in spans} == {trace_id}
    ids = {s.context.span_id for s in spans}
    roots = [s for s in spans if s.parent is None or s.parent.span_id not in ids]
    assert len(roots) == 1 and hexid(roots[0].parent.span_id, 16) == caller_span  # type: ignore[union-attr]

    gw = server_span(spans, "gateway")
    od = server_span(spans, "orders")
    inv = server_span(spans, "inventory")
    pay = server_span(spans, "payments")
    assert roots[0] is gw

    # gateway server -> gateway client -> orders server
    gw_call = parent_of(od, spans)
    assert gw_call.kind == SpanKind.CLIENT
    assert gw_call.resource.attributes["service.name"] == "gateway"
    assert parent_of(gw_call, spans) is gw

    # orders server -> orders client -> inventory server / payments server
    for downstream in (inv, pay):
        call = parent_of(downstream, spans)
        assert call.kind == SpanKind.CLIENT
        assert call.resource.attributes["service.name"] == "orders"
        assert parent_of(call, spans) is od

    # Work inside each service is a child of that service's server span.
    by_name = {s.name: s for s in spans}
    assert parent_of(by_name["reserve_stock"], spans) is inv
    assert parent_of(by_name["authorize_card"], spans) is pay

    # Inventory is called before payments (reserve, then charge).
    assert inv.start_time < pay.start_time
    assert not [s.name for s in spans if s.status.status_code == StatusCode.ERROR]


async def test_no_card_token_ever_appears_in_spans(shop: Shop) -> None:
    await shop.client.post("/checkout", json={**TEA_AND_MUG, "card_token": "tok_demo_visa"})
    for span in shop.spans.get_finished_spans():
        for value in (span.attributes or {}).values():
            assert "tok_demo" not in str(value), span.name


async def test_out_of_stock_returns_409_and_never_charges(shop: Shop) -> None:
    order = {"items": [{"sku": "beans", "qty": 20}, {"sku": "beans", "qty": 20}]}  # 40 > 30
    r = await shop.client.post("/checkout", json=order)

    assert r.status_code == 409
    assert r.json()["title"] == "Out of stock"
    services = {s.resource.attributes["service.name"] for s in shop.spans.get_finished_spans()}
    assert "payments" not in services
    assert shop.inventory.state.stock == INITIAL_STOCK


async def test_declined_card_returns_402_and_releases_stock(shop: Shop) -> None:
    r = await shop.client.post("/checkout", json={**TEA_AND_MUG, "card_token": "tok_demo_declined"})

    assert r.status_code == 402
    assert shop.inventory.state.stock == INITIAL_STOCK  # the reservation was given back
    assert shop.payments.state.charge_count == 0


async def test_payment_timeout_returns_504_marks_errors_and_releases_stock() -> None:
    s = await build_shop(payment_timeout=0.2)
    s.payments.state.delay_seconds = 1.0  # payments answers after orders has given up
    headers, _, _ = caller_headers()
    r = await s.client.post("/checkout", json=TEA_AND_MUG, headers=headers)
    await s.client.aclose()

    assert r.status_code == 504
    assert r.json()["title"] == "Payment timed out"
    spans = list(s.spans.get_finished_spans())
    od = server_span(spans, "orders")
    assert od.status.status_code == StatusCode.ERROR
    assert od.attributes["error.type"] == "payments_timeout"
    assert any(e.name == "exception" for e in od.events)
    assert server_span(spans, "gateway").status.status_code == StatusCode.ERROR
    assert s.inventory.state.stock == INITIAL_STOCK
    assert s.payments.state.charge_count == 0


async def test_inventory_down_returns_503() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    s = await build_shop(inventory_transport=httpx.MockTransport(refuse))
    r = await s.client.post("/checkout", json=TEA_AND_MUG)
    await s.client.aclose()

    assert r.status_code == 503
    assert r.json()["title"] == "Inventory unavailable"
    od = server_span(list(s.spans.get_finished_spans()), "orders")
    assert od.status.status_code == StatusCode.ERROR
    assert od.attributes["error.type"] == "inventory_unavailable"


async def test_same_idempotency_key_charges_once(shop: Shop) -> None:
    pay = httpx.AsyncClient(transport=httpx.ASGITransport(app=shop.payments), base_url="http://p")
    body = {"order_id": "o-1", "amount_paise": 12_000, "card_token": "tok_demo_visa"}
    key = {"Idempotency-Key": "order-o-1-key"}
    first, second = await asyncio.gather(
        pay.post("/charges", json=body, headers=key), pay.post("/charges", json=body, headers=key)
    )
    await pay.aclose()

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["charge_id"] == second.json()["charge_id"]
    assert shop.payments.state.charge_count == 1


async def test_charge_without_idempotency_key_is_refused(shop: Shop) -> None:
    pay = httpx.AsyncClient(transport=httpx.ASGITransport(app=shop.payments), base_url="http://p")
    body = {"order_id": "o-1", "amount_paise": 12_000, "card_token": "tok_demo_visa"}
    r = await pay.post("/charges", json=body)
    await pay.aclose()
    assert r.status_code == 422


async def test_concurrent_orders_never_oversell(shop: Shop) -> None:
    # 50 mugs in stock, 30 customers each want 2: exactly 25 can be served.
    order = {"items": [{"sku": "mug", "qty": 2}]}
    replies = await asyncio.gather(*(shop.client.post("/checkout", json=order) for _ in range(30)))
    codes = [r.status_code for r in replies]

    assert codes.count(201) == 25
    assert codes.count(409) == 5
    assert shop.inventory.state.stock["mug"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"items": []},
        {"items": [{"sku": "TEA", "qty": 1}]},
        {"items": [{"sku": "tea", "qty": 0}]},
        {"items": [{"sku": "tea", "qty": 1}], "card_token": "4111111111111111"},
    ],
)
async def test_bad_checkout_is_rejected_at_the_gateway(shop: Shop, body: dict[str, Any]) -> None:
    r = await shop.client.post("/checkout", json=body)
    assert r.status_code == 422
    services = {s.resource.attributes["service.name"] for s in shop.spans.get_finished_spans()}
    assert services == {"gateway"}  # nothing invalid travels past the front door
