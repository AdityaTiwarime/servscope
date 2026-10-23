"""W2 ingest: OTLP export requests to ServScope, as the Collector sends them.

Each simulated user is one exporter sending gzip-compressed batches back to back (a closed
loop), so the number of users is the number of concurrent exports. A batch is 20 checkouts'
worth of spans (180 spans across four services, with the attributes the real shop sets).

Every batch has fresh trace and span IDs: re-sending a body would hit the duplicate path,
which is far cheaper and would flatter the numbers. Bodies are built when this file is
imported, before Locust starts its run timer, so building them costs the measurement
nothing. (Building them in a test_start hook counted against the run time: a first version
did that, and its short runs sent nothing at all.) If the pool runs
out, the run stops and is marked invalid rather than silently re-sending duplicates.

    PERF_BODIES=4000 locust -f perf/locust_ingest.py --headless -u 8 -t 60s --host ...
"""

from __future__ import annotations

import gzip
import os
import random
import time
from collections import deque

import window  # noqa: F401  (marks the measured window for bench.py)
from locust import FastHttpUser, constant, events, task
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

CHECKOUTS_PER_BATCH = 20
SPANS_PER_BATCH = CHECKOUTS_PER_BATCH * 9
TOKEN = os.environ.get("SERVSCOPE_INGEST_TOKEN", "")
BODIES: deque[bytes] = deque()
EXHAUSTED = {"value": False}

# (service, name, kind, parent index, start ms, duration ms, attributes) for one checkout.
SHAPE = [
    (
        "gateway",
        "POST /checkout",
        2,
        None,
        0.0,
        14.0,
        {"http.request.method": "POST", "url.path": "/checkout", "http.response.status_code": 201},
    ),
    (
        "gateway",
        "POST orders",
        3,
        0,
        1.0,
        12.0,
        {"http.request.method": "POST", "server.address": "orders", "peer.service": "orders"},
    ),
    (
        "orders",
        "POST /orders",
        2,
        1,
        2.0,
        10.0,
        {"http.request.method": "POST", "url.path": "/orders", "http.response.status_code": 201},
    ),
    (
        "orders",
        "POST inventory",
        3,
        2,
        2.5,
        4.0,
        {"http.request.method": "POST", "server.address": "inventory", "peer.service": "inventory"},
    ),
    (
        "inventory",
        "POST /reservations",
        2,
        3,
        3.0,
        3.0,
        {
            "http.request.method": "POST",
            "url.path": "/reservations",
            "http.response.status_code": 201,
        },
    ),
    ("inventory", "reserve_stock", 1, 4, 3.5, 2.0, {"order.id": "x"}),
    (
        "orders",
        "POST payments",
        3,
        2,
        7.0,
        4.0,
        {"http.request.method": "POST", "server.address": "payments", "peer.service": "payments"},
    ),
    (
        "payments",
        "POST /charges",
        2,
        6,
        7.5,
        3.0,
        {"http.request.method": "POST", "url.path": "/charges", "http.response.status_code": 201},
    ),
    (
        "payments",
        "authorize_card",
        1,
        7,
        8.0,
        2.0,
        {"order.id": "x", "payment.amount_paise": 12000},
    ),
]


def _value(v: object) -> AnyValue:
    if isinstance(v, bool):
        return AnyValue(bool_value=v)
    if isinstance(v, int):
        return AnyValue(int_value=v)
    return AnyValue(string_value=str(v))


def build_batch(now_ns: int) -> bytes:
    by_service: dict[str, list[Span]] = {}
    for _ in range(CHECKOUTS_PER_BATCH):
        trace_id = os.urandom(16)
        ids = [os.urandom(8) for _ in SHAPE]
        start = now_ns - random.randint(0, 2_000_000_000)  # within the last 2 s, like a batch
        for i, (service, name, kind, parent, at_ms, dur_ms, attrs) in enumerate(SHAPE):
            s = Span(
                trace_id=trace_id, span_id=ids[i], name=name, kind=kind,
                parent_span_id=ids[parent] if parent is not None else b"",
                start_time_unix_nano=start + int(at_ms * 1e6),
                end_time_unix_nano=start + int((at_ms + dur_ms) * 1e6),
                attributes=[KeyValue(key=k, value=_value(v)) for k, v in attrs.items()],
                status=Status(code=Status.STATUS_CODE_UNSET),
            )  # fmt: skip
            by_service.setdefault(service, []).append(s)
    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(
                    attributes=[KeyValue(key="service.name", value=AnyValue(string_value=svc))]
                ),
                scope_spans=[ScopeSpans(spans=spans)],
            )
            for svc, spans in by_service.items()
        ]
    )
    return gzip.compress(request.SerializeToString(), compresslevel=6)  # the Collector gzips


def build_pool(n: int) -> None:
    started = time.time()
    now = time.time_ns()
    BODIES.extend(build_batch(now) for _ in range(n))
    print(
        f"built {n} bodies ({n * SPANS_PER_BATCH:,} spans) in {time.time() - started:.1f} s",
        flush=True,
    )


build_pool(int(os.environ.get("PERF_BODIES", "3000")))


@events.test_stop.add_listener
def report(environment, **_kwargs):  # type: ignore[no-untyped-def]
    if EXHAUSTED["value"]:
        print("INVALID RUN: the body pool ran out; raise PERF_BODIES", flush=True)


class Exporter(FastHttpUser):
    wait_time = constant(0)  # closed loop: the next export starts when the last one ends

    @task
    def export(self) -> None:
        try:
            body = BODIES.popleft()
        except IndexError:
            EXHAUSTED["value"] = True
            self.environment.runner.quit()
            return
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        }
        with self.client.post(
            "/v1/traces", data=body, headers=headers, name="export 180 spans", catch_response=True
        ) as r:
            if r.status_code == 503:
                r.failure("refused (503): over capacity")
            elif r.status_code != 200:
                r.failure(f"unexpected {r.status_code}")
