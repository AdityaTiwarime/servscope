"""spans table

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spans",
        sa.Column("trace_id", sa.String(32), primary_key=True),
        sa.Column("span_id", sa.String(16), primary_key=True),
        sa.Column("parent_span_id", sa.String(16)),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(256), nullable=False),
        sa.Column("kind", sa.SmallInteger, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_ns", sa.BigInteger, nullable=False),
        sa.Column("duration_ns", sa.BigInteger, nullable=False),
        sa.Column("status_code", sa.SmallInteger, nullable=False),
        sa.Column("status_message", sa.Text),
        sa.Column("http_status", sa.SmallInteger),
        sa.Column("attributes", postgresql.JSONB, nullable=False),
        sa.Column("attributes_truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_spans_service_start", "spans", ["service", sa.text("start_time DESC")])
    op.create_index(
        "ix_spans_errors_start",
        "spans",
        [sa.text("start_time DESC")],
        postgresql_where=sa.text("status_code = 2"),
    )


def downgrade() -> None:
    op.drop_table("spans")
