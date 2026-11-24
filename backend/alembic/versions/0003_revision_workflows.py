"""Persist revision workflows and single accepted answers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_revision_workflows"
down_revision = "0002_task_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "revision_workflows",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("question_id", sa.Uuid(), unique=True),
        sa.Column("question_evidence", postgresql.JSONB(none_as_null=True)),
        sa.ForeignKeyConstraint(
            ["task_id", "revision"],
            [f"{schema}.task_revisions.task_id", f"{schema}.task_revisions.revision"],
        ),
        sa.CheckConstraint(
            "stage IN ('awaiting_clarification', 'ready_for_extraction')",
            name="ck_revision_workflows_stage",
        ),
        sa.CheckConstraint(
            "(question_id IS NULL) = (question_evidence IS NULL) AND "
            "(stage <> 'awaiting_clarification' OR question_id IS NOT NULL)",
            name="ck_revision_workflows_question",
        ),
        schema=schema,
    )
    op.create_table(
        "workflow_answers",
        sa.Column(
            "question_id", sa.Uuid(), sa.ForeignKey(f"{schema}.revision_workflows.question_id"),
            primary_key=True,
        ),
        sa.Column("answer", sa.String(8), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("answer IN ('ex_gst', 'inc_gst')", name="ck_workflow_answers_value"),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    quoted_schema = op.get_context().dialect.identifier_preparer.quote_identifier(schema)
    # These tables are optional because the checkpoint setup is separate from Alembic.
    # Remove only this workflow's threads, including starts without an application row.
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        op.execute(
            f"""
            DO $cleanup$
            BEGIN
                IF to_regclass('{quoted_schema}.{table}') IS NOT NULL THEN
                    DELETE FROM {quoted_schema}.{table} AS checkpoint
                    USING {quoted_schema}.task_revisions AS revision
                    WHERE checkpoint.thread_id =
                        concat('invoice:', revision.task_id, ':', revision.revision);
                END IF;
            END
            $cleanup$;
            """
        )
    op.drop_table("workflow_answers", schema=schema)
    op.drop_table("revision_workflows", schema=schema)
