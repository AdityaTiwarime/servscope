"""POST /v1/traces: the OTLP/HTTP endpoint the Collector exports to.

Order of checks, cheapest first: token, content type, size, decompression, decoding,
then a write slot, then one database transaction. A batch is acknowledged (200) only after
it is committed, so the Collector re-sends anything that was not.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import zlib
from typing import Any

from fastapi import APIRouter, Request, Response
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from .models import Span
from .otlp import MalformedPayload, decode_traces

log = logging.getLogger("servscope.ingest")
router = APIRouter()

PROTOBUF = "application/x-protobuf"
# asyncpg allows 32,767 parameters per statement; a span row uses 15.
_ROWS_PER_INSERT = 1000
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


async def _store(request: Request, rows: list[dict[str, Any]]) -> int:
    """Insert all rows in one transaction; returns how many were new (not duplicates)."""
    inserted = 0
    async with request.app.state.sessionmaker() as session, session.begin():
        for i in range(0, len(rows), _ROWS_PER_INSERT):
            stmt = (
                insert(Span)
                .values(rows[i : i + _ROWS_PER_INSERT])
                .on_conflict_do_nothing(index_elements=["trace_id", "span_id"])
                .returning(Span.span_id)
            )
            inserted += len((await session.execute(stmt)).all())
    return inserted


@router.post("/v1/traces")
async def export_traces(request: Request) -> Response:
    settings = request.app.state.settings
    try:
        _check_token(request)
        if request.headers.get("content-type", "").split(";")[0].strip() != PROTOBUF:
            raise IngestError(415, _INVALID_ARGUMENT, f"content-type must be {PROTOBUF}")
        raw = await _read_body(request, settings.max_body_bytes)
        encoding = request.headers.get("content-encoding", "")
        body = _decompress(raw, encoding, settings.max_body_bytes)
        try:
            result = decode_traces(body, settings.max_attributes_bytes)
        except MalformedPayload as exc:
            raise IngestError(400, _INVALID_ARGUMENT, str(exc)) from None

        if result.rows:
            async with request.app.state.write_slots:
                try:
                    inserted = await _store(request, result.rows)
                except (SQLAlchemyError, OSError) as exc:
                    log.warning("span insert failed, asking the sender to retry: %s", exc)
                    raise IngestError(503, _UNAVAILABLE, "storage unavailable", 5) from None
            log.info(
                "stored spans",
                extra={"received": len(result.rows), "new": inserted, "rejected": result.rejected},
            )
    except IngestError as err:
        return _status_response(err)

    reply = ExportTraceServiceResponse()
    if result.rejected:
        reply.partial_success.rejected_spans = result.rejected
        reply.partial_success.error_message = "; ".join(result.reasons)
    return Response(reply.SerializeToString(), media_type=PROTOBUF)
