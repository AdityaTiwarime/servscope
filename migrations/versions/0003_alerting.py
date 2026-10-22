"""alert rules, alert states, incidents and their events, audit log, settings

Revision ID: 0003
Revises: 0002

Seeds four starting rules. The error and latency rules watch the gateway, i.e. what the
customer experiences: one slow payments service should open one incident about checkouts,
not one per service it ripples through. The traces linked from that incident lead to the
cause. "Unavailable" watches every service, because silence anywhere is worth knowing about.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NOW = sa.func.now()
DEFAULT_RULES = [
    # name, kind, service, threshold, window_s, for_s, recover_s
    ("Checkout errors", "error_rate", "gateway", 0.10, 120, 30, 60),
    ("Checkout latency (p95)", "p95_latency", "gateway", 1000.0, 120, 30, 60),
    ("Service unavailable", "unavailable", None, 0.0, 120, 0, 30),
    ("Checkout failures rising", "failure_rise", "gateway", 3.0, 300, 60, 120),
]


def upgrade() -> None:
    rules = op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("service", sa.String(128)),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("window_s", sa.Integer, nullable=False),
        sa.Column("for_s", sa.Integer, nullable=False),
        sa.Column("recover_s", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "kind IN ('error_rate', 'p95_latency', 'unavailable', 'failure_rise')",
            name="ck_alert_rules_kind",
        ),
    )
    op.create_table(
        "alert_states",
        sa.Column(
            "rule_id",
            sa.Integer,
            sa.ForeignKey("alert_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("service", sa.String(128), primary_key=True),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.Integer, sa.ForeignKey("alert_rules.id", ondelete="SET NULL")),
        sa.Column("rule_name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("peak_value", sa.Float),
    )
    op.create_index(
        "ux_incidents_one_open",
        "incidents",
        ["rule_id", "service"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index("ix_incidents_opened", "incidents", [sa.text("opened_at DESC"), "id"])
    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("value", sa.Float),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "sample_trace_ids",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_index("ix_incident_events_incident", "incident_events", ["incident_id", "time"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("details", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_audit_time", "audit_log", [sa.text("time DESC")])
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    cols = ("name", "kind", "service", "threshold", "window_s", "for_s", "recover_s")
    op.bulk_insert(rules, [dict(zip(cols, r, strict=True)) for r in DEFAULT_RULES])


def downgrade() -> None:
    for table in (
        "app_settings",
        "audit_log",
        "incident_events",
        "incidents",
        "alert_states",
        "alert_rules",
    ):
        op.drop_table(table)
