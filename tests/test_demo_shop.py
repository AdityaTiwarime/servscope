"""The four demo services wired together in one process.

Every service has its own tracer and they talk over real HTTP semantics (httpx in-process
transports), so these tests check the trace exactly as the services produce it: one trace,
four services, and every parent-child link where it should be.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from opentelemetry import context as otel_context
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from demo_services import gateway, inventory, orders, payments
from demo_services.faults import Faults, FaultSpec
from demo_services.inventory import INITIAL_STOCK
from demo_services.telemetry import Exporters


class IsolatedASGITransport(httpx.ASGITransport):
    """An in-process transport that behaves like a network hop.

    Two things a plain ASGITransport gets wrong, both of which hid real bugs:

    - It runs the called app in the caller's context, so the called service would "see" the
      caller's span without any header, which would hide a broken propagation. Here each call
      runs in an empty OpenTelemetry context: `traceparent` is the only way across.
    - When the caller gives up (a timeout cancels it), it cancels the called app too, so a
      slow payments call simply stopped. Over a real network the callee never hears about
      the timeout and finishes its work. Here each call runs in its own task that the
      caller's cancellation cannot reach, so a late charge really happens, as in production.
    """

    def __init__(self, app: Any):
        super().__init__(app=app)
        self.pending: set[asyncio.Task[httpx.Response]] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def call() -> httpx.Response:
            token = otel_context.attach(otel_context.Context())
            try:
                return await super(IsolatedASGITransport, self).handle_async_request(request)
            finally:
                otel_context.detach(token)

        task = asyncio.create_task(call())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return await asyncio.shield(task)


@dataclass
class Shop:
    client: httpx.AsyncClient
    spans: InMemorySpanExporter
    logs: InMemoryLogRecordExporter
    inventory: Any
    payments: Any
    hops: list[IsolatedASGITransport]
    payments_faults: Faults
    orders_faults: Faults

    async def settle(self) -> None:
        """Wait until every service has finished, including work nobody waits for any more."""
        while pending := [t for hop in self.hops for t in hop.pending]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        await self.client.aclose()
        await self.settle()


async def build_shop(
    payment_timeout: float = 2.0,
    inventory_transport: httpx.AsyncBaseTransport | None = None,
    payments_transport: Callable[[Any], httpx.AsyncBaseTransport] | None = None,
) -> Shop:
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    exporters = Exporters(spans, logs)
    pay_faults, ord_faults = Faults(), Faults()
    inv = inventory.create_app(exporters)
    pay = payments.create_app(exporters, faults=pay_faults)
    to_inv, to_pay = IsolatedASGITransport(app=inv), IsolatedASGITransport(app=pay)
    orders_app = orders.create_app(
        "http://inventory",
        "http://payments",
        exporters,
        inventory_transport=inventory_transport or to_inv,
        payments_transport=payments_transport(to_pay) if payments_transport else to_pay,
        payment_timeout=payment_timeout,
        faults=ord_faults,
    )
    to_ord = IsolatedASGITransport(app=orders_app)
    gw = gateway.create_app("http://orders", exporters, to_ord)
    to_gw = IsolatedASGITransport(app=gw)
    client = httpx.AsyncClient(transport=to_gw, base_url="http://gateway")
    return Shop(
        client, spans, logs, inv, pay, [to_inv, to_pay, to_ord, to_gw], pay_faults, ord_faults
    )


@pytest.fixture
async def shop() -> AsyncIterator[Shop]:
    s = await build_shop()
    yield s
    await s.close()


def slow_payments(shop: Shop, seconds: float) -> None:
    shop.payments_faults.set(FaultSpec(delay_ms=round(seconds * 1000), duration_s=60))


def caller_headers() -> tuple[dict[str, str], str, str]:
    trace_id, span_id = os.urandom(16).hex(), os.urandom(8).hex()
    return {"traceparent": f"00-{trace_id}-{span_id}-01"}, trace_id, span_id


def hexid(n: int, width: int) -> str:
    return format(n, f"0{width}x")


def server_span(spans: list[ReadableSpan], service: str, name: str | None = None) -> ReadableSpan:
    """The one server span of `service` (optionally the one named `name`, e.g. "POST /charges")."""
    [s] = [
        s
        for s in spans
        if s.kind == SpanKind.SERVER
        and s.resource.attributes["service.name"] == service
        and (name is None or s.name == name)
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
    slow_payments(s, 1.0)  # payments would answer after orders has given up
    headers, _, _ = caller_headers()
    r = await s.client.post("/checkout", json=TEA_AND_MUG, headers=headers)
    await s.close()

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
    await s.close()

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


def log_lines(shop: Shop) -> list[Any]:
    return [entry.log_record for entry in shop.logs.get_finished_logs()]


async def test_log_lines_carry_their_request_trace_and_span(shop: Shop) -> None:
    headers, trace_id, _ = caller_headers()
    r = await shop.client.post("/checkout", json=TEA_AND_MUG, headers=headers)
    assert r.status_code == 201

    spans = list(shop.spans.get_finished_spans())
    span_ids = {s.context.span_id for s in spans}
    lines = log_lines(shop)
    assert lines, "the services wrote no logs"
    for line in lines:
        assert hexid(line.trace_id, 32) == trace_id  # every line belongs to this checkout
        assert line.span_id in span_ids  # ...and to a span that exists in it
    messages = [str(line.body) for line in lines]
    assert any("placed" in m for m in messages)
    assert any("charged" in m for m in messages)
    # The service a line came from travels on its resource, checked end to end in smoke.py.


async def test_payment_timeout_is_explained_in_the_logs() -> None:
    s = await build_shop(payment_timeout=0.2)
    slow_payments(s, 1.0)
    await s.client.post("/checkout", json=TEA_AND_MUG)
    await s.close()
    errors = [str(line.body) for line in log_lines(s) if line.severity_text == "ERROR"]
    assert any("payments timed out after 0.2s" in m for m in errors)
    assert any("released reservation" in str(line.body) for line in log_lines(s))


async def test_no_card_token_ever_appears_in_logs(shop: Shop) -> None:
    await shop.client.post("/checkout", json={**TEA_AND_MUG, "card_token": "tok_demo_declined"})
    await shop.client.post("/checkout", json={**TEA_AND_MUG, "card_token": "tok_demo_visa"})
    lines = log_lines(shop)
    assert any("declined" in str(line.body) for line in lines)
    for line in lines:
        assert "tok_demo" not in str(line.body)
        assert "tok_demo" not in str(dict(line.attributes or {}))


def client_span_to(spans: list[ReadableSpan], callee: str) -> ReadableSpan:
    """The caller's span for the main POST to `callee` (not a void or a release)."""
    main_path = {"payments": "/charges", "inventory": "/reservations", "orders": "/orders"}
    [s] = [
        s
        for s in spans
        if s.kind == SpanKind.CLIENT
        and (s.attributes or {}).get("server.address") == callee
        and (s.attributes or {}).get("url.path") == main_path[callee]
    ]
    return s


async def test_a_timed_out_call_is_an_error_on_the_callers_side() -> None:
    # The callee may still answer 201 later; the caller's span must already say "timeout".
    s = await build_shop(payment_timeout=0.2)
    slow_payments(s, 1.0)
    await s.client.post("/checkout", json=TEA_AND_MUG)
    await s.close()
    call = client_span_to(list(s.spans.get_finished_spans()), "payments")
    assert call.status.status_code == StatusCode.ERROR
    assert (call.attributes or {})["error.type"] == "timeout"
    assert any(e.name == "exception" for e in call.events)


async def test_a_declined_card_is_an_answer_not_a_broken_call(shop: Shop) -> None:
    await shop.client.post("/checkout", json={**TEA_AND_MUG, "card_token": "tok_demo_declined"})
    call = client_span_to(list(shop.spans.get_finished_spans()), "payments")
    assert call.status.status_code != StatusCode.ERROR
    assert (call.attributes or {})["http.response.status_code"] == 402


async def test_unreachable_service_is_an_error_with_its_cause() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    s = await build_shop(inventory_transport=httpx.MockTransport(refuse))
    await s.client.post("/checkout", json=TEA_AND_MUG)
    await s.close()
    call = client_span_to(list(s.spans.get_finished_spans()), "inventory")
    assert call.status.status_code == StatusCode.ERROR
    assert (call.attributes or {})["error.type"] == "ConnectError"


# --- the double charge (found in Milestone 4, fixed in Milestone 5) ---------------------


async def test_a_timed_out_order_is_never_charged() -> None:
    """Regression: payments used to finish the charge after orders had answered 504."""
    s = await build_shop(payment_timeout=0.2)
    slow_payments(s, 1.0)
    r = await s.client.post("/checkout", json=TEA_AND_MUG)
    assert r.status_code == 504
    await s.close()  # wait for payments too, the part nobody is waiting for

    assert s.payments.state.charge_count == 0
    assert s.payments.state.abandoned_count == 1  # it stopped at the caller's deadline
    pay = server_span(list(s.spans.get_finished_spans()), "payments", "POST /charges")
    assert (pay.attributes or {})["payment.abandoned"] == "deadline_exceeded"
    assert pay.end_time - pay.start_time < 0.6e9  # gave up at ~0.2 s, not after the full 1 s
    assert s.inventory.state.stock == INITIAL_STOCK


class WithoutBudget(httpx.AsyncBaseTransport):
    """A caller that sends no time budget, so payments cannot know when to give up. This is
    the race the void exists for: the charge completes after the caller stopped waiting."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request.headers["X-Request-Timeout-Ms"]
        return await self.inner.handle_async_request(request)


async def test_a_charge_arriving_after_the_void_is_refused() -> None:
    s = await build_shop(payment_timeout=0.2, payments_transport=WithoutBudget)
    slow_payments(s, 1.0)
    r = await s.client.post("/checkout", json=TEA_AND_MUG)
    assert r.status_code == 504
    await s.close()

    assert s.payments.state.charge_count == 0
    messages = [str(line.body) for line in log_lines(s)]
    assert any("refused: the order was already cancelled" in m for m in messages)


async def test_a_void_reverses_a_completed_charge_once(shop: Shop) -> None:
    pay = httpx.AsyncClient(transport=httpx.ASGITransport(app=shop.payments), base_url="http://p")
    body = {"order_id": "o-9", "amount_paise": 12_000, "card_token": "tok_demo_visa"}
    key = {"Idempotency-Key": "order-o-9-key"}
    charged = await pay.post("/charges", json=body, headers=key)
    first = await pay.post("/charges/void", headers=key)
    again = await pay.post("/charges/void", headers=key)  # a retried void changes nothing
    replay = await pay.post("/charges", json=body, headers=key)
    await pay.aclose()

    assert charged.status_code == 201
    assert first.json() == {"voided": True, "charge_id": charged.json()["charge_id"]}
    assert again.json()["voided"] is False
    assert replay.status_code == 409  # a cancelled order cannot be charged again
    assert (shop.payments.state.charge_count, shop.payments.state.void_count) == (1, 1)


# --- the fault switch used by Demo scenarios ------------------------------------------

FAULT_TOKEN = "fault-token-for-tests"


async def test_fault_switch_does_not_exist_without_its_token(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEMO_FAULT_TOKEN", raising=False)
    pay = httpx.AsyncClient(transport=httpx.ASGITransport(app=shop.payments), base_url="http://p")
    r = await pay.put("/_faults", json={"delay_ms": 3000, "duration_s": 60})
    await pay.aclose()
    assert r.status_code == 404


async def test_fault_switch_needs_the_token_and_switches_itself_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_FAULT_TOKEN", FAULT_TOKEN)
    now = [1000.0]
    faults = Faults(clock=lambda: now[0])
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    app = payments.create_app(Exporters(spans, logs), faults=faults)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
    spec = {"delay_ms": 1500, "duration_s": 60}

    assert (await c.put("/_faults", json=spec)).status_code == 401
    bad = {"X-Fault-Token": "wrong"}
    assert (await c.put("/_faults", json=spec, headers=bad)).status_code == 401
    good = {"X-Fault-Token": FAULT_TOKEN}
    too_long = {"delay_ms": 1500, "duration_s": 601}
    assert (await c.put("/_faults", json=too_long, headers=good)).status_code == 422

    r = await c.put("/_faults", json=spec, headers=good)
    assert r.status_code == 200
    assert r.json()["active"]["delay_ms"] == 1500
    now[0] += 59
    assert (await c.get("/_faults", headers=good)).json()["remaining_s"] == 1.0
    now[0] += 1  # the duration runs out on the service's own clock
    assert (await c.get("/_faults", headers=good)).json()["active"] is None
    await c.aclose()
    assert spans.get_finished_spans() == ()  # flipping the switch is not shop traffic
    assert any("demo fault switched ON" in str(e.log_record.body) for e in logs.get_finished_logs())


async def test_payments_error_fault_fails_checkouts_without_charging(shop: Shop) -> None:
    shop.payments_faults.set(FaultSpec(error_rate=1.0, duration_s=60))
    r = await shop.client.post("/checkout", json=TEA_AND_MUG)
    await shop.settle()

    assert r.status_code == 502
    pay = server_span(list(shop.spans.get_finished_spans()), "payments")
    assert pay.status.status_code == StatusCode.ERROR
    assert (pay.attributes or {})["demo.fault"] == "error"
    assert shop.payments.state.charge_count == 0
    assert shop.inventory.state.stock == INITIAL_STOCK


async def test_cut_off_inventory_is_a_real_connection_failure(shop: Shop) -> None:
    shop.orders_faults.set(FaultSpec(unreachable=["inventory"], duration_s=60))
    r = await shop.client.post("/checkout", json=TEA_AND_MUG)
    await shop.settle()

    assert r.status_code == 503
    spans = list(shop.spans.get_finished_spans())
    call = client_span_to(spans, "inventory")
    assert (call.attributes or {})["error.type"] == "ConnectError"  # refused by the OS
    services = {s.resource.attributes["service.name"] for s in spans}
    assert "inventory" not in services  # nothing reached it


async def test_inventory_restocks_on_its_schedule() -> None:
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    app = inventory.create_app(Exporters(spans, logs), restock_every_s=0.05)
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://i")
        r = await c.post(
            "/reservations", json={"order_id": "o1", "items": [{"sku": "mug", "qty": 5}]}
        )
        assert r.status_code == 201
        assert app.state.stock["mug"] == INITIAL_STOCK["mug"] - 5
        await asyncio.sleep(0.15)
        assert app.state.stock == INITIAL_STOCK
        await c.aclose()
