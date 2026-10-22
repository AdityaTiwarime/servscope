"""Database tables: spans, logs and per-minute metric rollups. Incidents arrive in Milestone 5."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Span(Base):
    __tablename__ = "spans"

    # (trace_id, span_id) is the identity of a span in OpenTelemetry, so it is the primary key.
    # A retried delivery of the same span hits this key and is ignored (ON CONFLICT DO NOTHING).
    # Because trace_id comes first, the key also serves "all spans of one trace" lookups.
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))

    service: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(256))
    kind: Mapped[int] = mapped_column(SmallInteger)

    # start_time is for indexing and time windows; start_ns keeps the exact nanoseconds,
    # which timestamptz (microseconds) would round.
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_ns: Mapped[int] = mapped_column(BigInteger)
    duration_ns: Mapped[int] = mapped_column(BigInteger)

    status_code: Mapped[int] = mapped_column(SmallInteger)  # 0 unset, 1 ok, 2 error (OTLP)
    status_message: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)

    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attributes_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_spans_start", text("start_time DESC")),
        Index("ix_spans_service_start", "service", text("start_time DESC")),
        Index(
            "ix_spans_errors_start",
            text("start_time DESC"),
            postgresql_where=text("status_code = 2"),
        ),
    )


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Logs have no natural ID, so a retried batch is recognised by a hash of what makes a log
    # line itself: service, exact time, trace/span and text. Two genuinely identical lines in
    # the same nanosecond from the same span collapse into one, an accepted trade-off.
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary(16), unique=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_ns: Mapped[int] = mapped_column(BigInteger)
    service: Mapped[str] = mapped_column(String(128))
    severity_number: Mapped[int] = mapped_column(SmallInteger)  # OTLP: 9 INFO, 13 WARN, 17 ERROR
    severity_text: Mapped[str] = mapped_column(String(16))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_logs_trace", "trace_id", postgresql_where=text("trace_id IS NOT NULL")),
        Index("ix_logs_service_time", "service", text("time DESC")),
        Index("ix_logs_time", text("time DESC")),
        Index(
            "ix_logs_message_fts",
            text("to_tsvector('simple', message)"),
            postgresql_using="gin",
        ),
    )


class MetricRollup(Base):
    """Requests, errors and a latency histogram per service, operation and minute,
    built from server spans as they are ingested."""

    __tablename__ = "metric_rollups"

    service: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(256), primary_key=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer)
    error_count: Mapped[int] = mapped_column(Integer)
    latency_buckets: Mapped[list[int]] = mapped_column(ARRAY(Integer))

    __table_args__ = (Index("ix_rollups_minute", "minute"),)
