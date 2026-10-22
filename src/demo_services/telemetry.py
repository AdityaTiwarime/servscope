"""OpenTelemetry setup shared by every demo service: traces and logs.

Each service gets its own providers (never the global ones), so that all four services can
run inside one test process and still report their own service.name.

In production, spans and logs are exported in the background by batch processors with
bounded queues, so a slow or missing Collector never slows a customer request: when a queue
is full, new records are dropped (and counted by the SDK) instead of blocking.

Log lines written while a request is being handled automatically carry that request's
trace_id and span_id. That is what lets ServScope jump from a trace to its logs.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

BACKSTOP_TIMEOUT = httpx.Timeout(10.0)


@dataclass
class Exporters:
    """Where telemetry goes. Tests pass in-memory exporters; None means the Collector."""

    spans: SpanExporter
    logs: LogRecordExporter


def _endpoint(path: str) -> str:
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    return f"{base.rstrip('/')}{path}"


def setup_telemetry(
    app: FastAPI, service: str, exporters: Exporters | None = None
) -> tuple[TracerProvider, logging.Logger]:
    """Instrument `app` as `service`; returns its tracer provider and its logger."""
    resource = Resource.create({"service.name": service})

    tracer_provider = TracerProvider(resource=resource)
    logger_provider = LoggerProvider(resource=resource)
    if exporters is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporters.spans))
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(exporters.logs))
    else:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_endpoint("/v1/traces"), timeout=5),
                max_queue_size=2048,
                schedule_delay_millis=1000,
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=_endpoint("/v1/logs"), timeout=5),
                max_queue_size=2048,
                schedule_delay_millis=1000,
            )
        )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="healthz",
        exclude_spans=["receive", "send"],  # one span per request, not three
    )

    logger = logging.getLogger(f"demo.{service}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # create_app can run many times in one test process: replace, don't stack, handlers.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(console)
    return tracer_provider, logger


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
