"""Persist QA runs and exact candidate and preview identities for workbook delivery."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_workbook_qa_runs"
down_revision = "0009_workbook_repair_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "workbook_qa_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("qa_version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=False),
        sa.Column("candidate", postgresql.JSONB()),
        sa.Column("preview", postgresql.JSONB()),
        sa.Column("owner_review", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], [f"{schema}.workbook_artifacts.artifact_id"]),
        sa.CheckConstraint(
            "status IN ('blocked', 'awaiting_visual_review', 'ready_for_delivery')",
            name="ck_workbook_qa_status",
        ),
        schema=schema,
    )
    op.create_index("ix_workbook_qa_artifact", "workbook_qa_runs", ["artifact_id", "qa_version", "created_at"], schema=schema)


def downgrade() -> None:
    op.drop_table("workbook_qa_runs", schema=op.get_context().opts["version_table_schema"])
