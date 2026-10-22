"""Database tables: telemetry (spans, logs, rollups), alerting (rules, states, incidents) and
administration (audit log, settings)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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


RULE_KINDS = ("error_rate", "p95_latency", "unavailable", "failure_rise")
_KIND_CHECK = "kind IN ('error_rate', 'p95_latency', 'unavailable', 'failure_rise')"


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    service: Mapped[str | None] = mapped_column(String(128))  # None: every service
    threshold: Mapped[float] = mapped_column(Float)
    window_s: Mapped[int] = mapped_column(Integer)
    for_s: Mapped[int] = mapped_column(Integer)
    recover_s: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint(_KIND_CHECK, name="ck_alert_rules_kind"),)


class AlertState(Base):
    """Where each (rule, service) pair is in its state machine. Kept in the database so a
    restart resumes a half-elapsed `for`/`recover` timer instead of starting it again."""

    __tablename__ = "alert_states"

    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True
    )
    service: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(12))  # ok pending firing recovering no_data
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float | None] = mapped_column(Float)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # The rule may be edited or deleted later; the incident keeps what it was opened under.
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="SET NULL"))
    rule_name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    service: Mapped[str] = mapped_column(String(128))
    threshold: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(10))  # open, resolved
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    peak_value: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        # The deduplication rule: at most one OPEN incident per rule and service. Repeated
        # breaches become events on that incident instead of new incidents.
        Index(
            "ux_incidents_one_open",
            "rule_id",
            "service",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        Index("ix_incidents_opened", text("opened_at DESC"), "id"),
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # opened peak breached_again no_data data_back acknowledged resolved
    type: Mapped[str] = mapped_column(String(20))
    value: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str] = mapped_column(Text, server_default="")
    sample_trace_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), server_default=text("'{}'")
    )

    __table_args__ = (Index("ix_incident_events_incident", "incident_id", "time"),)


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (Index("ix_audit_time", text("time DESC")),)


class AppSetting(Base):
    """Settings changed from the dashboard (retention days). They override the environment
    defaults until changed again."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
