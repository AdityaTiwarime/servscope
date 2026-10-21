"""Builds OTLP trace exports for tests, the same bytes the Collector would send."""

from __future__ import annotations

import os
import time
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status


def kv(key: str, value: Any) -> KeyValue:
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def make_span(
    *,
    trace_id: bytes | None = None,
    span_id: bytes | None = None,
    parent: bytes = b"",
    name: str = "GET /items",
    start_ns: int | None = None,
    duration_ns: int = 5_000_000,
    error: bool = False,
    attrs: dict[str, Any] | None = None,
) -> Span:
    start = start_ns or time.time_ns()
    return Span(
        trace_id=trace_id if trace_id is not None else os.urandom(16),
        span_id=span_id if span_id is not None else os.urandom(8),
        parent_span_id=parent,
        name=name,
        kind=Span.SPAN_KIND_SERVER,
        start_time_unix_nano=start,
        end_time_unix_nano=start + duration_ns,
        status=Status(code=Status.STATUS_CODE_ERROR if error else Status.STATUS_CODE_OK),
        attributes=[kv(k, v) for k, v in (attrs or {}).items()],
    )


def make_export(spans: list[Span], service: str | None = "gateway") -> bytes:
    resource_attrs = [kv("service.name", service)] if service else []
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource={"attributes": resource_attrs},
                scope_spans=[ScopeSpans(spans=spans)],
            )
        ]
    )
    return req.SerializeToString()
