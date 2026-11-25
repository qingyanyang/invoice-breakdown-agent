"""Allow a new immutable version to repair a missing workbook for the same inputs."""

from alembic import op

revision = "0009_workbook_repair_versions"
down_revision = "0008_workbook_artifacts"
branch_labels = None
depends_on = None

INPUTS = ["task_id", "revision", "source_sha256", "allocation_id", "generator_version", "template_version"]


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_constraint("uq_workbook_artifact_inputs", "workbook_artifacts", type_="unique", schema=schema)
    op.create_index("ix_workbook_artifact_inputs", "workbook_artifacts", [*INPUTS, "version"], schema=schema)


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    # PostgreSQL rejects this downgrade if immutable repair versions share an identity.
    op.create_unique_constraint("uq_workbook_artifact_inputs", "workbook_artifacts", INPUTS, schema=schema)
    op.drop_index("ix_workbook_artifact_inputs", table_name="workbook_artifacts", schema=schema)
