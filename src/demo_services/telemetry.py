"""OpenTelemetry setup shared by every demo service.

Spans are exported in the background by a BatchSpanProcessor with a bounded queue, so a
slow or missing Collector never slows a customer request: when the queue is full, new spans
are dropped (and counted by the SDK) instead of blocking.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(app: FastAPI, service: str) -> TracerProvider:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", timeout=5),
            max_queue_size=2048,
            schedule_delay_millis=1000,
        )
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="healthz")
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return provider
