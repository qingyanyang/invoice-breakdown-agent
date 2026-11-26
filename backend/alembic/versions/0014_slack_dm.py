"""Persist Slack DM events, task bindings, approvals, and delivery receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_slack_dm"
down_revision = "0013_task_rule_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "slack_inbox",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("event", JSONB, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("error", sa.String(64)),
        schema=schema,
    )
    op.create_index("ix_slack_inbox_available", "slack_inbox", ["state", "available_at"], schema=schema)
    op.create_table(
        "slack_threads",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("binding", JSONB, nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey(f"{schema}.tasks.id"), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "slack_controls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("thread_key", sa.String(64), sa.ForeignKey(f"{schema}.slack_threads.key"), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )
    op.create_table(
        "slack_visual_votes",
        sa.Column("thread_key", sa.String(64), sa.ForeignKey(f"{schema}.slack_threads.key"), primary_key=True),
        sa.Column("qa_id", sa.Uuid(), primary_key=True),
        sa.Column("sheet", sa.String(31), primary_key=True),
        sa.Column("outcome", sa.String(8), nullable=False),
        schema=schema,
    )
    op.create_table(
        "slack_files",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("thread_key", sa.String(64), sa.ForeignKey(f"{schema}.slack_threads.key"), nullable=False),
        sa.Column("file_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    for name in ("slack_files", "slack_visual_votes", "slack_controls", "slack_threads", "slack_inbox"):
        op.drop_table(name, schema=schema)
