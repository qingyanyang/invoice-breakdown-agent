"""Store revision-bound trade and area assignments and clarification evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_source_classifications"
down_revision = "0005_source_reconciliations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.add_column(
        "source_reconciliations",
        sa.Column("generation", sa.Uuid(), server_default=sa.func.gen_random_uuid(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "source_classifications",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("extractor_version", sa.String(40), primary_key=True),
        sa.Column("reconciliation_generation", sa.Uuid(), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("classifier_version", sa.String(40), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("skill_sha256", sa.String(64)),
        sa.Column("model", sa.String(255)),
        sa.Column("proposal", postgresql.JSONB()),
        sa.Column("decisions", postgresql.JSONB(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "revision", "extractor_version"],
            [f"{schema}.source_reconciliations.{name}" for name in ("task_id", "revision", "extractor_version")],
        ),
        sa.CheckConstraint(
            "status IN ('failed', 'blocked', 'ready_for_allocation')", name="ck_source_classifications_status",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_table("source_classifications", schema=schema)
    op.drop_column("source_reconciliations", "generation", schema=schema)
