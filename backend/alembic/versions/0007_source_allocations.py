"""Persist allocation reports for a verified classification."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_source_allocations"
down_revision = "0006_source_classifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    classified_key = ["task_id", "revision", "extractor_version", "reconciliation_generation"]
    op.create_table(
        "source_allocations",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("extractor_version", sa.String(40), primary_key=True),
        sa.Column("reconciliation_generation", sa.Uuid(), primary_key=True),
        sa.Column("classification_id", sa.Uuid(), primary_key=True),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("allocator_version", sa.String(40), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(classified_key, [f"{schema}.source_classifications.{key}" for key in classified_key]),
        sa.CheckConstraint("status IN ('blocked', 'ready_for_workbook')", name="ck_source_allocations_status"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("source_allocations", schema=op.get_context().opts["version_table_schema"])
