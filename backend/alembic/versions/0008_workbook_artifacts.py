"""Persist immutable workbook versions and exact generation input identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_workbook_artifacts"
down_revision = "0007_source_allocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "workbook_artifacts",
        sa.Column("artifact_id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("generator_version", sa.String(40), nullable=False),
        sa.Column("template_version", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(110), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "revision"],
            [f"{schema}.task_revisions.task_id", f"{schema}.task_revisions.revision"],
        ),
        sa.UniqueConstraint("task_id", "version", name="uq_workbook_artifact_version"),
        sa.UniqueConstraint(
            "task_id", "revision", "source_sha256", "allocation_id", "generator_version", "template_version",
            name="uq_workbook_artifact_inputs",
        ),
        sa.CheckConstraint("version > 0 AND size > 0", name="ck_workbook_artifact_size_version"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("workbook_artifacts", schema=op.get_context().opts["version_table_schema"])
