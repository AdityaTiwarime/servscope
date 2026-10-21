"""OpenTelemetry setup shared by every demo service.

Each service gets its own TracerProvider (never the global one), so that all four services
can run inside one test process and still report their own service.name.

In production, spans are exported in the background by a BatchSpanProcessor with a bounded
queue, so a slow or missing Collector never slows a customer request: when the queue is full,
new spans are dropped (and counted by the SDK) instead of blocking.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

BACKSTOP_TIMEOUT = httpx.Timeout(10.0)


def setup_tracing(
    app: FastAPI, service: str, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Instrument `app` as `service`. Tests pass an in-memory exporter; otherwise spans go
    to the Collector at OTEL_EXPORTER_OTLP_ENDPOINT."""
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", timeout=5),
                max_queue_size=2048,
                schedule_delay_millis=1000,
            )
        )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="healthz",
        exclude_spans=["receive", "send"],  # one span per request, not three
    )
    return provider


def traced_client(
    provider: TracerProvider,
    base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """An HTTP client whose calls become client spans and carry the `traceparent` header.

    Callers set the real deadline with asyncio.timeout() around each call, which also works
    with the in-process transports tests use. The 10 s network timeout is only a backstop, so
    a call someone forgets to wrap still can't hang for ever."""
    client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=BACKSTOP_TIMEOUT)
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)
    return client
