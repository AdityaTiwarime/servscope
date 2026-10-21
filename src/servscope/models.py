"""Database tables. Milestone 1 has only `spans`; the rest of the model arrives in Milestone 3."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
        Index("ix_spans_service_start", "service", text("start_time DESC")),
        Index(
            "ix_spans_errors_start",
            text("start_time DESC"),
            postgresql_where=text("status_code = 2"),
        ),
    )
