"""Store revision-bound source reconciliation and clarification evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_source_reconciliations"
down_revision = "0004_source_extractions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "source_reconciliations",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("extractor_version", sa.String(40), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("decisions", postgresql.JSONB(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "revision", "extractor_version"],
            [f"{schema}.source_extractions.{name}" for name in ("task_id", "revision", "extractor_version")],
        ),
        sa.CheckConstraint(
            "status IN ('blocked', 'ready_for_classification')",
            name="ck_source_reconciliations_status",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_table("source_reconciliations", schema=schema)
