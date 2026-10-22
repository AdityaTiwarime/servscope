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

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
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
from opentelemetry.trace import SpanKind, Status, StatusCode

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


class Downstream:
    """Calls one downstream service. Each call is a CLIENT span that carries `traceparent`.

    Why not the generic httpx instrumentation: when a call is cut off by asyncio.timeout,
    that instrumentation ends its span without marking an error (cancellation is not an
    ordinary exception), so a timeout looked like success from the caller's side, which is
    exactly the failure an observability tool must show. Here the span is ours, so:

    - a timeout or a connection failure is an error, with `error.type`;
    - a 5xx answer is an error;
    - a 4xx answer is NOT: a declined card or an out-of-stock reply is the other service
      working correctly. (The OpenTelemetry convention suggests marking client 4xx as
      errors; this demo deliberately keeps "refused" apart from "broken".)
    """

    def __init__(
        self,
        provider: TracerProvider,
        name: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.tracer = provider.get_tracer("demo.downstream")
        # The 10 s network timeout is only a backstop; each call passes its real deadline.
        self.client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=BACKSTOP_TIMEOUT
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        deadline_s: float,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        with self.tracer.start_as_current_span(
            f"{method} {self.name}",
            kind=SpanKind.CLIENT,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("server.address", self.name)
            span.set_attribute("url.path", path)
            outgoing = dict(headers or {})
            propagate.inject(outgoing)  # traceparent of THIS span, so the callee is its child
            try:
                async with asyncio.timeout(deadline_s):
                    response = await self.client.request(method, path, headers=outgoing, **kwargs)
            except TimeoutError as exc:
                span.record_exception(exc)
                span.set_attribute("error.type", "timeout")
                span.set_status(Status(StatusCode.ERROR, f"no answer within {deadline_s}s"))
                raise
            except httpx.HTTPError as exc:
                span.record_exception(exc)
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, f"{self.name} unreachable"))
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_attribute("error.type", str(response.status_code))
                span.set_status(Status(StatusCode.ERROR))
            return response
