"""Pin approved primary and supplementary rules to an effective task revision."""

import sqlalchemy as sa
from alembic import op

revision = "0013_task_rule_pins"
down_revision = "0012_execution_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "task_rule_pins",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("primary_sha256", sa.String(64), nullable=False),
        sa.Column("primary_version", sa.String(96), nullable=False),
        sa.Column("supplementary_sha256", sa.String(64)),
        sa.Column("supplementary_version", sa.String(96)),
        sa.ForeignKeyConstraint(
            ["task_id", "revision"], [f"{schema}.task_revisions.task_id", f"{schema}.task_revisions.revision"],
        ),
        sa.CheckConstraint(
            "(supplementary_sha256 IS NULL) = (supplementary_version IS NULL)",
            name="ck_task_rule_supplementary",
        ),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("task_rule_pins", schema=op.get_context().opts["version_table_schema"])
