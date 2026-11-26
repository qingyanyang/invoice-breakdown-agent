from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, String, Table, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

workbook_qa_runs = Table(
    "workbook_qa_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("artifact_id", Uuid, ForeignKey("workbook_artifacts.artifact_id"), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("qa_version", String(40), nullable=False),
    Column("status", String(32), nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("candidate", JSONB),
    Column("preview", JSONB),
    Column("owner_review", JSONB),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Index("ix_workbook_qa_artifact", "artifact_id", "qa_version", "created_at"),
    CheckConstraint(
        "status IN ('blocked', 'awaiting_visual_review', 'ready_for_delivery')",
        name="ck_workbook_qa_status",
    ),
)
