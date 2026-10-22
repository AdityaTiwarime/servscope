"""logs, metric rollups, and a time index on spans

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_spans_start", "spans", [sa.text("start_time DESC")])

    op.create_table(
        "logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("fingerprint", sa.LargeBinary(16), nullable=False, unique=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_ns", sa.BigInteger, nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("severity_number", sa.SmallInteger, nullable=False),
        sa.Column("severity_text", sa.String(16), nullable=False),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("span_id", sa.String(16)),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("attributes", postgresql.JSONB, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_logs_trace", "logs", ["trace_id"], postgresql_where=sa.text("trace_id IS NOT NULL")
    )
    op.create_index("ix_logs_service_time", "logs", ["service", sa.text("time DESC")])
    op.create_index("ix_logs_time", "logs", [sa.text("time DESC")])
    op.create_index(
        "ix_logs_message_fts",
        "logs",
        [sa.text("to_tsvector('simple', message)")],
        postgresql_using="gin",
    )

    op.create_table(
        "metric_rollups",
        sa.Column("service", sa.String(128), primary_key=True),
        sa.Column("operation", sa.String(256), primary_key=True),
        sa.Column("minute", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("request_count", sa.Integer, nullable=False),
        sa.Column("error_count", sa.Integer, nullable=False),
        sa.Column("latency_buckets", postgresql.ARRAY(sa.Integer), nullable=False),
    )
    op.create_index("ix_rollups_minute", "metric_rollups", ["minute"])


def downgrade() -> None:
    op.drop_table("metric_rollups")
    op.drop_table("logs")
    op.drop_index("ix_spans_start", table_name="spans")
