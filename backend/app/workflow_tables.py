from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String,
    Table, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

revision_workflows = Table(
    "revision_workflows",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("stage", String(24), nullable=False),
    Column("question_id", Uuid, unique=True),
    Column("question_evidence", JSONB(none_as_null=True)),
    ForeignKeyConstraint(["task_id", "revision"], ["task_revisions.task_id", "task_revisions.revision"]),
    CheckConstraint(
        "stage IN ('awaiting_clarification', 'ready_for_extraction')",
        name="ck_revision_workflows_stage",
    ),
    CheckConstraint(
        "(question_id IS NULL) = (question_evidence IS NULL) AND "
        "(stage <> 'awaiting_clarification' OR question_id IS NOT NULL)",
        name="ck_revision_workflows_question",
    ),
)

workflow_answers = Table(
    "workflow_answers",
    metadata,
    Column("question_id", Uuid, ForeignKey("revision_workflows.question_id"), primary_key=True),
    Column("answer", String(8), nullable=False),
    Column("accepted_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("answer IN ('ex_gst', 'inc_gst')", name="ck_workflow_answers_value"),
)
