"""Add owned tasks and immutable input revisions."""

from alembic import op
import sqlalchemy as sa

revision = "0002_task_sources"
down_revision = "0001_database_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("current_revision >= 1", name="ck_tasks_current_revision"),
        sa.CheckConstraint("operation = 'create'", name="ck_tasks_operation"),
        schema=schema,
    )
    op.create_table(
        "task_revisions",
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey(f"{schema}.tasks.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("source_key", sa.String(110)),
        sa.Column("source_filename", sa.String(255)),
        sa.Column("source_size", sa.Integer()),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("target_amount", sa.Numeric(18, 2)),
        sa.Column("target_gst_basis", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_task_revisions_revision"),
        sa.CheckConstraint(
            "(target_amount IS NULL AND target_gst_basis IS NULL) OR "
            "(target_amount IS NOT NULL AND target_amount >= 0 AND "
            "target_gst_basis IS NOT NULL AND target_gst_basis IN "
            "('excluding_gst', 'including_gst', 'unknown'))",
            name="ck_task_revisions_target",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_source' AND source_key IS NULL AND source_filename IS NULL "
            "AND source_size IS NULL AND source_sha256 IS NULL) OR "
            "(status = 'source_uploaded' AND source_key IS NOT NULL AND source_filename IS NOT NULL "
            "AND source_size IS NOT NULL AND source_size > 0 AND source_sha256 IS NOT NULL)",
            name="ck_task_revisions_source",
        ),
        schema=schema,
    )
    op.create_index("ix_task_revisions_source_key", "task_revisions", ["source_key"], schema=schema)


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_table("task_revisions", schema=schema)
    op.drop_table("tasks", schema=schema)
