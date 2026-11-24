from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKeyConstraint, Integer, String, Table, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

source_reconciliations = Table(
    "source_reconciliations",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("extractor_version", String(40), primary_key=True),
    Column("source_sha256", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("scope", JSONB, nullable=False),
    Column("decisions", JSONB, nullable=False),
    Column("report", JSONB, nullable=False),
    Column("recorded_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    ForeignKeyConstraint(
        ["task_id", "revision", "extractor_version"],
        ["source_extractions.task_id", "source_extractions.revision", "source_extractions.extractor_version"],
    ),
    CheckConstraint("status IN ('blocked', 'ready_for_classification')", name="ck_source_reconciliations_status"),
)
