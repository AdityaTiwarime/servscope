"""Unit tests for OTLP decoding: no database needed."""

import json
import os

import pytest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from otlp_builders import make_export, make_span

from servscope.otlp import (
    UNKNOWN_SERVICE,
    MalformedPayload,
    any_value,
    cap_attributes,
    decode_traces,
    ns_to_datetime,
)

LIMIT = 8192


def test_decodes_a_span_into_a_row() -> None:
    tid, sid, parent = os.urandom(16), os.urandom(8), os.urandom(8)
    span = make_span(
        trace_id=tid,
        span_id=sid,
        parent=parent,
        name="POST /checkout",
        start_ns=1_760_000_000_123_456_789,
        duration_ns=42_000_000,
        error=True,
        attrs={"http.response.status_code": 504, "cart.items": 3},
    )
    result = decode_traces(make_export([span], service="gateway"), LIMIT)

    assert result.rejected == 0
    [row] = result.rows
    assert row["trace_id"] == tid.hex() and len(row["trace_id"]) == 32
    assert row["span_id"] == sid.hex()
    assert row["parent_span_id"] == parent.hex()
    assert row["service"] == "gateway"
    assert row["operation"] == "POST /checkout"
    assert row["start_ns"] == 1_760_000_000_123_456_789
    assert row["duration_ns"] == 42_000_000
    assert row["status_code"] == 2
    assert row["http_status"] == 504
    assert row["attributes"]["cart.items"] == 3


def test_root_span_has_no_parent() -> None:
    [row] = decode_traces(make_export([make_span()]), LIMIT).rows
    assert row["parent_span_id"] is None


def test_older_http_status_attribute_is_understood() -> None:
    [row] = decode_traces(make_export([make_span(attrs={"http.status_code": 404})]), LIMIT).rows
    assert row["http_status"] == 404


def test_missing_service_name_is_labelled_not_dropped() -> None:
    [row] = decode_traces(make_export([make_span()], service=None), LIMIT).rows
    assert row["service"] == UNKNOWN_SERVICE


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"trace_id": b"\x01" * 8}, "trace_id"),
        ({"trace_id": b"\x00" * 16}, "trace_id"),
        ({"span_id": b"\x01" * 4}, "span_id"),
        ({"parent": b"\x01" * 3}, "parent_span_id"),
        ({"duration_ns": -1}, "ends before"),
    ],
)
def test_one_invalid_span_is_rejected_and_the_rest_kept(bad: dict, reason: str) -> None:
    good = make_span(name="good")
    result = decode_traces(make_export([good, make_span(**bad)]), LIMIT)
    assert [r["operation"] for r in result.rows] == ["good"]
    assert result.rejected == 1
    assert reason in result.reasons[0]


def test_garbage_body_is_malformed() -> None:
    with pytest.raises(MalformedPayload):
        decode_traces(b"\xff\xff\xff not protobuf", LIMIT)


def test_empty_body_is_an_empty_export() -> None:
    result = decode_traces(b"", LIMIT)
    assert result.rows == [] and result.rejected == 0


def test_oversized_attributes_drop_the_largest_keys_first() -> None:
    attrs = {"small": "x", "huge": "y" * 10_000, "medium": "z" * 500}
    kept, truncated = cap_attributes(attrs, 1_000)
    assert truncated
    assert "huge" not in kept
    assert kept["small"] == "x" and kept["medium"] == "z" * 500
    assert len(json.dumps(kept)) <= 1_000


def test_small_attributes_are_untouched() -> None:
    attrs = {"a": 1}
    assert cap_attributes(attrs, 1_000) == (attrs, False)


def test_truncation_is_flagged_on_the_row() -> None:
    span = make_span(attrs={"payload": "x" * (LIMIT * 2)})
    [row] = decode_traces(make_export([span]), LIMIT).rows
    assert row["attributes_truncated"] is True
    assert "payload" not in row["attributes"]


def test_nanoseconds_convert_without_float_error() -> None:
    # 2025-10-09T08:53:20.123456789Z; a float division would drift in the last digits.
    dt = ns_to_datetime(1_760_000_000_123_456_789)
    assert dt.isoformat() == "2025-10-09T08:53:20.123456+00:00"


def test_nested_attribute_values() -> None:
    value = AnyValue(
        kvlist_value=KeyValueList(
            values=[
                KeyValue(
                    key="list",
                    value=AnyValue(
                        array_value=ArrayValue(
                            values=[AnyValue(int_value=1), AnyValue(string_value="two")]
                        )
                    ),
                ),
                KeyValue(key="raw", value=AnyValue(bytes_value=b"\xab\xcd")),
                KeyValue(key="empty", value=AnyValue()),
            ]
        )
    )
    assert any_value(value) == {"list": [1, "two"], "raw": "abcd", "empty": None}
