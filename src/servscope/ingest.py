"""POST /v1/traces and /v1/logs: the OTLP/HTTP endpoints the Collector exports to.

Order, cheapest first: token, content type, reading the body (size-capped), then a write
slot, and only then the CPU-heavy part (unzipping, decoding) and one database transaction.
A batch is acknowledged (200) only after it is committed, so the Collector re-sends anything
that was not.

Why the slot comes before decoding (found by the Milestone 6 flood drill): decoding a big
batch is CPU work. Done first, every batch in a flood was decoded, even the ones about to be
refused, and that work ran on the event loop, so ServScope stopped answering anything else
(/healthz took 7 s). Now a batch over capacity is refused before it costs any CPU, and the
decoding runs in a worker thread, so the event loop stays free for other requests.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from . import rollups
from .models import Log, Span
from .otlp import DecodeResult, MalformedPayload, decode_logs, decode_traces
from .stats import Stats

log = logging.getLogger("servscope.ingest")
router = APIRouter()

PROTOBUF = "application/x-protobuf"
Decoder = Callable[[bytes, int], DecodeResult]

# Rows are sent as an executemany: SQLAlchemy compiles the INSERT once (cached) and batches
# the rows itself. Building one giant INSERT ... VALUES per batch instead cost ~226 ms of CPU
# on the event loop per 1,000 spans, against ~18 ms this way (measured in Milestone 6).
_SPAN_INSERT = (
    insert(Span)
    .on_conflict_do_nothing(index_elements=["trace_id", "span_id"])
    .returning(
        Span.service, Span.operation, Span.kind, Span.start_ns, Span.duration_ns, Span.status_code
    )
)
_LOG_INSERT = insert(Log).on_conflict_do_nothing(index_elements=["fingerprint"]).returning(Log.id)
# gRPC status codes, which OTLP/HTTP error bodies use.
_INVALID_ARGUMENT, _RESOURCE_EXHAUSTED, _UNAUTHENTICATED, _UNAVAILABLE = 3, 8, 16, 14


class IngestError(Exception):
    def __init__(self, http_status: int, grpc_code: int, message: str, retry_after: int = 0):
        super().__init__(message)
        self.http_status, self.grpc_code, self.retry_after = http_status, grpc_code, retry_after


class WriteSlots:
    """Caps concurrent database writes. Waiting is bounded: a batch that can't get a slot in
    time is refused with 503, which the Collector treats as 'retry later'. That is the
    backpressure: memory use stays flat instead of queueing without limit."""

    def __init__(self, slots: int, wait_seconds: float):
        self.total = slots
        self.wait_seconds = wait_seconds
        self._sem = asyncio.Semaphore(slots)
        self.in_use = 0
        self.refused = 0

    async def __aenter__(self) -> WriteSlots:
        try:
            await asyncio.wait_for(self._sem.acquire(), self.wait_seconds)
        except TimeoutError:
            self.refused += 1
            raise IngestError(503, _UNAVAILABLE, "all write slots busy", retry_after=1) from None
        self.in_use += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.in_use -= 1
        self._sem.release()


def _status_response(err: IngestError) -> Response:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    body = Status(code=err.grpc_code, message=str(err)).SerializeToString()
    return Response(body, status_code=err.http_status, media_type=PROTOBUF, headers=headers)


def _check_token(request: Request) -> None:
    expected = request.app.state.settings.ingest_token.get_secret_value()
    given = request.headers.get("authorization", "")
    scheme, _, token = given.partition(" ")
    # compare_digest takes the same time whatever the input, so the token can't be guessed
    # one character at a time by measuring response times.
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), expected.encode()):
        raise IngestError(401, _UNAUTHENTICATED, "missing or wrong ingest token")


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise IngestError(413, _RESOURCE_EXHAUSTED, f"body larger than {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():  # counted as it arrives: Content-Length can lie
        size += len(chunk)
        if size > limit:
            raise IngestError(413, _RESOURCE_EXHAUSTED, f"body larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _decompress(body: bytes, encoding: str, limit: int) -> bytes:
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise IngestError(415, _INVALID_ARGUMENT, f"unsupported content-encoding {encoding!r}")
    # The limit applies to the unzipped size too, or a 1 MB "zip bomb" could expand to GBs.
    inflater = zlib.decompressobj(wbits=31)
    try:
        out = inflater.decompress(body, limit + 1)
    except zlib.error:
        raise IngestError(400, _INVALID_ARGUMENT, "body is not valid gzip") from None
    if len(out) > limit or inflater.unconsumed_tail:
        raise IngestError(413, _RESOURCE_EXHAUSTED, f"unzipped body larger than {limit} bytes")
    return out


async def _store_spans(request: Request, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Spans and their metric rollups in ONE transaction: either both are saved or neither,
    so a crash between them can't leave metrics that disagree with the stored spans.
    Returns (new spans, late spans)."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as session, session.begin():
        # RETURNING with ON CONFLICT DO NOTHING returns only the rows actually inserted, so
        # a retried batch contributes nothing to the metrics below.
        new = [dict(r._mapping) for r in await session.execute(_SPAN_INSERT, rows)]
        result = rollups.aggregate(new, datetime.now(UTC), settings.late_metrics_after)
        await rollups.upsert(session, result.aggregates)
    return len(new), result.late


async def _store_logs(request: Request, rows: list[dict[str, Any]]) -> int:
    async with request.app.state.sessionmaker() as session, session.begin():
        inserted = len((await session.execute(_LOG_INSERT, rows)).all())
    return inserted


async def _receive(request: Request) -> bytes:
    """The cheap checks, and the size-capped body. No CPU-heavy work yet."""
    settings = request.app.state.settings
    _check_token(request)
    if request.headers.get("content-type", "").split(";")[0].strip() != PROTOBUF:
        raise IngestError(415, _INVALID_ARGUMENT, f"content-type must be {PROTOBUF}")
    return await _read_body(request, settings.max_body_bytes)


def _decode(
    raw: bytes, encoding: str, limit: int, attr_limit: int, decoder: Decoder
) -> DecodeResult:
    """Unzip and decode: plain CPU work, run in a worker thread."""
    body = _decompress(raw, encoding, limit)
    try:
        return decoder(body, attr_limit)
    except MalformedPayload as exc:
        raise IngestError(400, _INVALID_ARGUMENT, str(exc)) from None


async def _ingest(
    request: Request,
    decoder: Decoder,
    what: str,
    store: Callable[[list[dict[str, Any]]], Awaitable[Any]],
) -> tuple[DecodeResult, Any]:
    """Read, take a write slot, decode in a thread, store. Returns what was decoded and what
    the store function returned (None when there was nothing to store)."""
    settings = request.app.state.settings
    stats: Stats = request.app.state.stats
    raw = await _receive(request)
    encoding = request.headers.get("content-encoding", "")
    try:
        async with request.app.state.write_slots:
            result = await asyncio.to_thread(
                _decode,
                raw,
                encoding,
                settings.max_body_bytes,
                settings.max_attributes_bytes,
                decoder,
            )
            if not result.rows:
                return result, None
            try:
                return result, await store(result.rows)
            except (SQLAlchemyError, OSError) as exc:
                log.warning("%s insert failed, asking the sender to retry: %s", what, exc)
                raise IngestError(503, _UNAVAILABLE, "storage unavailable", 5) from None
    except IngestError as err:
        if err.http_status == 503:  # busy or storage down: the sender will retry it
            stats.add("servscope_ingest_refused_total")
        raise


@router.post("/v1/traces")
async def export_traces(request: Request) -> Response:
    stats: Stats = request.app.state.stats
    try:
        result, stored = await _ingest(
            request, decode_traces, "span", lambda rows: _store_spans(request, rows)
        )
        if stored is not None:
            new, late = stored
            stats.add("servscope_spans_received_total", len(result.rows))
            stats.add("servscope_spans_stored_total", new)
            stats.add("servscope_spans_duplicate_total", len(result.rows) - new)
            stats.add("servscope_spans_late_total", late)
        stats.add("servscope_spans_rejected_total", result.rejected)
    except IngestError as err:
        return _status_response(err)

    reply = ExportTraceServiceResponse()
    if result.rejected:
        reply.partial_success.rejected_spans = result.rejected
        reply.partial_success.error_message = "; ".join(result.reasons)
    return Response(reply.SerializeToString(), media_type=PROTOBUF)


@router.post("/v1/logs")
async def export_logs(request: Request) -> Response:
    stats: Stats = request.app.state.stats
    try:
        result, new = await _ingest(
            request, decode_logs, "log", lambda rows: _store_logs(request, rows)
        )
        if new is not None:
            stats.add("servscope_logs_received_total", len(result.rows))
            stats.add("servscope_logs_stored_total", new)
            stats.add("servscope_logs_duplicate_total", len(result.rows) - new)
        stats.add("servscope_logs_rejected_total", result.rejected)
    except IngestError as err:
        return _status_response(err)

    reply = ExportLogsServiceResponse()
    if result.rejected:
        reply.partial_success.rejected_log_records = result.rejected
        reply.partial_success.error_message = "; ".join(result.reasons)
    return Response(reply.SerializeToString(), media_type=PROTOBUF)
