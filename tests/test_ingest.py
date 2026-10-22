"""Integration tests for POST /v1/traces against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import gzip
import os
from typing import Any

import pytest
from conftest import TOKEN, count_spans
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from otlp_builders import make_export, make_span

AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-protobuf"}


async def post(client: Any, body: bytes, headers: dict[str, str] | None = None) -> Any:
    return await client.post("/v1/traces", content=body, headers=headers or AUTH)


async def test_spans_are_stored_and_acknowledged(make_client: Any, clean_db: str) -> None:
    tid = os.urandom(16)
    root, child = os.urandom(8), os.urandom(8)
    body = make_export(
        [
            make_span(trace_id=tid, span_id=root, name="POST /checkout"),
            make_span(trace_id=tid, span_id=child, parent=root, name="price_cart"),
        ]
    )
    async with make_client() as client:
        r = await post(client, body)

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-protobuf"
    reply = ExportTraceServiceResponse.FromString(r.content)
    assert reply.partial_success.rejected_spans == 0
    assert await count_spans(clean_db) == 2


async def test_same_batch_twice_is_stored_once(make_client: Any, clean_db: str) -> None:
    # This is what a Collector retry looks like after a lost acknowledgement.
    body = make_export([make_span(), make_span()])
    async with make_client() as client:
        assert (await post(client, body)).status_code == 200
        assert (await post(client, body)).status_code == 200
    assert await count_spans(clean_db) == 2


async def test_bad_span_is_reported_and_the_rest_stored(make_client: Any, clean_db: str) -> None:
    body = make_export([make_span(), make_span(span_id=b"\x01\x02")])
    async with make_client() as client:
        r = await post(client, body)
    reply = ExportTraceServiceResponse.FromString(r.content)
    assert r.status_code == 200
    assert reply.partial_success.rejected_spans == 1
    assert "span_id" in reply.partial_success.error_message
    assert await count_spans(clean_db) == 1


@pytest.mark.parametrize(
    "auth", [None, "Bearer wrong-token", f"Basic {TOKEN}", "Bearer", f"Bearer {TOKEN}x"]
)
async def test_wrong_or_missing_token_is_refused(
    make_client: Any, clean_db: str, auth: str | None
) -> None:
    headers = {"Content-Type": "application/x-protobuf"}
    if auth:
        headers["Authorization"] = auth
    async with make_client() as client:
        r = await post(client, make_export([make_span()]), headers)
    assert r.status_code == 401
    assert Status.FromString(r.content).code == 16  # UNAUTHENTICATED
    assert await count_spans(clean_db) == 0


async def test_json_content_type_is_refused(make_client: Any) -> None:
    async with make_client() as client:
        r = await post(client, b"{}", {**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 415


async def test_oversized_body_is_refused(make_client: Any, clean_db: str) -> None:
    body = make_export([make_span(attrs={"blob": "x" * 5_000})])
    async with make_client(max_body_bytes=2_000) as client:
        r = await post(client, body)
    assert r.status_code == 413
    assert await count_spans(clean_db) == 0


async def test_gzip_body_is_accepted(make_client: Any, clean_db: str) -> None:
    body = gzip.compress(make_export([make_span()]))
    async with make_client() as client:
        r = await post(client, body, {**AUTH, "Content-Encoding": "gzip"})
    assert r.status_code == 200
    assert await count_spans(clean_db) == 1


async def test_gzip_bomb_is_refused(make_client: Any) -> None:
    bomb = gzip.compress(b"\x00" * 50_000_000)  # ~50 KB on the wire, 50 MB unzipped
    assert len(bomb) < 100_000
    async with make_client() as client:
        r = await post(client, bomb, {**AUTH, "Content-Encoding": "gzip"})
    assert r.status_code == 413


async def test_broken_gzip_is_a_bad_request(make_client: Any) -> None:
    async with make_client() as client:
        r = await post(client, b"not gzip at all", {**AUTH, "Content-Encoding": "gzip"})
    assert r.status_code == 400


async def test_unknown_encoding_is_refused(make_client: Any) -> None:
    async with make_client() as client:
        r = await post(client, b"x", {**AUTH, "Content-Encoding": "br"})
    assert r.status_code == 415


async def test_garbage_protobuf_is_a_bad_request(make_client: Any, clean_db: str) -> None:
    async with make_client() as client:
        r = await post(client, b"\xff\xff\xff\xff")
    assert r.status_code == 400
    assert Status.FromString(r.content).code == 3  # INVALID_ARGUMENT


async def test_busy_write_slots_answer_503_with_retry_after(
    make_client: Any, clean_db: str
) -> None:
    async with make_client(write_slots=1, slot_wait_seconds=0.05) as client:
        slots = client.app.state.write_slots
        async with slots:  # another batch is holding the only slot
            r = await post(client, make_export([make_span()]))
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        assert slots.refused == 1
        # Once the slot is free, the Collector's retry succeeds.
        assert (await post(client, make_export([make_span()]))).status_code == 200
    assert await count_spans(clean_db) == 1


async def test_concurrent_batches_are_all_stored(make_client: Any, clean_db: str) -> None:
    bodies = [make_export([make_span() for _ in range(20)]) for _ in range(25)]
    async with make_client(write_slots=4, slot_wait_seconds=10) as client:
        replies = await asyncio.gather(*(post(client, b) for b in bodies))
    assert {r.status_code for r in replies} == {200}
    assert await count_spans(clean_db) == 500


async def test_database_down_asks_the_sender_to_retry(clean_db: str) -> None:
    from conftest import client_for, settings

    unreachable = "postgresql+asyncpg://servscope:servscope@127.0.0.1:1/servscope"
    async with client_for(settings(unreachable)) as client:
        r = await post(client, make_export([make_span()]))
        ready = await client.get("/readyz")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert ready.status_code == 503
    assert ready.json()["database"] == "unavailable"


async def test_ready_when_database_reachable(make_client: Any) -> None:
    async with make_client() as client:
        r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"database": "ok", "write_slots_in_use": 0, "write_slots_total": 8}


async def test_request_id_is_echoed_or_replaced(make_client: Any) -> None:
    async with make_client() as client:
        echoed = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
        replaced = await client.get("/healthz", headers={"X-Request-ID": "<script>"})
    assert echoed.headers["x-request-id"] == "abc-123"
    assert replaced.headers["x-request-id"] != "<script>"
    assert len(replaced.headers["x-request-id"]) == 32


async def test_api_docs_are_off_in_production(clean_db: str) -> None:
    from conftest import client_for, settings

    async with client_for(settings(clean_db, env="production")) as client:
        assert (await client.get("/docs")).status_code == 404


async def test_a_refused_batch_costs_no_decoding(make_client: Any, clean_db: str) -> None:
    """Regression (Milestone 6 flood drill): batches were decoded before a write slot was
    taken, so a flood decoded everything, even what it was about to refuse, and starved the
    event loop. Proof that the slot now comes first: with the only slot taken, even a body
    that is not valid protobuf gets 503 (busy), not 400 (malformed): it was never decoded."""
    async with make_client(write_slots=1, slot_wait_seconds=0.05) as client:
        async with client.app.state.write_slots:
            r = await post(client, b"this is not protobuf at all")
        assert r.status_code == 503
        assert (await post(client, b"this is not protobuf at all")).status_code == 400
