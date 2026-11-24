"""Store versioned workbook evidence for immutable task revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_source_extractions"
down_revision = "0003_revision_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "source_extractions",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("extractor_version", sa.String(40), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("error_code", sa.String(32)),
        sa.Column("error_detail", sa.String(255)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "revision"],
            [f"{schema}.task_revisions.task_id", f"{schema}.task_revisions.revision"],
        ),
        sa.CheckConstraint(
            "(status = 'extracted' AND result IS NOT NULL AND error_code IS NULL AND error_detail IS NULL) OR "
            "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL AND error_detail IS NOT NULL)",
            name="ck_source_extractions_outcome",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_table("source_extractions", schema=schema)
