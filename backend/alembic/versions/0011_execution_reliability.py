"""Persist idempotent mutation receipts and workflow execution ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_execution_reliability"
down_revision = "0010_workbook_qa_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "request_receipts",
        sa.Column("operator", sa.String(128), primary_key=True),
        sa.Column("operation", sa.String(40), primary_key=True),
        sa.Column("key_sha256", sa.String(64), primary_key=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("response", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("worker_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_expires_at", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(60)),
        sa.Column("exhaustion_reason", sa.String(24)),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id", "revision"], [f"{schema}.task_revisions.task_id", f"{schema}.task_revisions.revision"]),
        sa.UniqueConstraint("operator", "operation", "task_id", "revision", "input_sha256", name="uq_execution_inputs"),
        sa.CheckConstraint("attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts", name="ck_execution_attempt"),
        sa.CheckConstraint(
            "status IN ('running', 'retryable', 'succeeded', 'blocked', 'failed', 'exhausted')",
            name="ck_execution_status",
        ),
        schema=schema,
    )
    op.create_index("ix_execution_task", "execution_runs", ["task_id", "revision", "updated_at"], schema=schema)
    op.create_table(
        "execution_objects",
        sa.Column("storage_key", sa.String(150), primary_key=True),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey(f"{schema}.execution_runs.id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    for table in ("execution_objects", "execution_runs", "request_receipts"):
        op.drop_table(table, schema=schema)
