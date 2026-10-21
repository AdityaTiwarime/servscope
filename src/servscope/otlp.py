"""Turns an OTLP trace export (protobuf) into rows for the `spans` table.

Kept free of the database and of FastAPI so it can be unit-tested on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PbSpan

UNKNOWN_SERVICE = "unknown_service"
_MAX_SERVICE = 128
_MAX_OPERATION = 256


class MalformedPayload(ValueError):
    """The body is not a valid ExportTraceServiceRequest."""


@dataclass
class DecodeResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        if len(self.reasons) < 5:  # enough to debug, without echoing a whole bad batch back
            self.reasons.append(reason)


def any_value(v: AnyValue) -> Any:
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return attributes_to_dict(v.kvlist_value.values)
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind)


def attributes_to_dict(attrs: Any) -> dict[str, Any]:
    kvs: list[KeyValue] = list(attrs)
    return {kv.key: any_value(kv.value) for kv in kvs}


def cap_attributes(attrs: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], bool]:
    """Keep attributes under max_bytes of JSON. Drops whole keys (largest first) rather than
    cutting values in half, so what is kept is still accurate."""
    size = len(json.dumps(attrs, default=str))
    if size <= max_bytes:
        return attrs, False
    kept = dict(attrs)
    for key in sorted(attrs, key=lambda k: len(json.dumps(attrs[k], default=str)), reverse=True):
        del kept[key]
        if len(json.dumps(kept, default=str)) <= max_bytes:
            break
    return kept, True


def ns_to_datetime(ns: int) -> datetime:
    """Integer maths, not ns / 1e9: a float can't hold today's epoch in nanoseconds exactly."""
    seconds, rest = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(microseconds=rest // 1000)


def _http_status(attrs: dict[str, Any]) -> int | None:
    # Current semantic convention first, then the older name some libraries still send.
    for key in ("http.response.status_code", "http.status_code"):
        value = attrs.get(key)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def _span_row(span: PbSpan, service: str, max_attr_bytes: int) -> dict[str, Any]:
    if len(span.trace_id) != 16 or not any(span.trace_id):
        raise ValueError("trace_id must be 16 non-zero bytes")
    if len(span.span_id) != 8 or not any(span.span_id):
        raise ValueError("span_id must be 8 non-zero bytes")
    if span.parent_span_id and len(span.parent_span_id) != 8:
        raise ValueError("parent_span_id must be 8 bytes")
    if span.start_time_unix_nano == 0:
        raise ValueError("start time missing")
    if span.end_time_unix_nano < span.start_time_unix_nano:
        raise ValueError("span ends before it starts")

    attrs = attributes_to_dict(span.attributes)
    capped, truncated = cap_attributes(attrs, max_attr_bytes)
    start_ns = span.start_time_unix_nano
    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": span.parent_span_id.hex() or None,
        "service": service[:_MAX_SERVICE],
        "operation": (span.name or "unnamed")[:_MAX_OPERATION],
        "kind": span.kind,
        "start_time": ns_to_datetime(start_ns),
        "start_ns": start_ns,
        "duration_ns": span.end_time_unix_nano - start_ns,
        "status_code": span.status.code,
        "status_message": span.status.message or None,
        "http_status": _http_status(attrs),
        "attributes": capped,
        "attributes_truncated": truncated,
    }


def decode_traces(body: bytes, max_attr_bytes: int) -> DecodeResult:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:  # protobuf raises its own DecodeError type
        raise MalformedPayload("body is not an OTLP ExportTraceServiceRequest") from exc

    result = DecodeResult()
    for resource_spans in request.resource_spans:
        resource = attributes_to_dict(resource_spans.resource.attributes)
        service = resource.get("service.name")
        if not isinstance(service, str) or not service:
            service = UNKNOWN_SERVICE
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                try:
                    result.rows.append(_span_row(span, service, max_attr_bytes))
                except ValueError as exc:
                    # One bad span must not cost the rest of the batch.
                    result.reject(str(exc))
    return result
