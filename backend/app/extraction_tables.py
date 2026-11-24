from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKeyConstraint, Integer, String, Table, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

source_extractions = Table(
    "source_extractions",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("extractor_version", String(40), primary_key=True),
    Column("source_sha256", String(64), nullable=False),
    Column("status", String(12), nullable=False),
    Column("result", JSONB(none_as_null=True)),
    Column("error_code", String(32)),
    Column("error_detail", String(255)),
    Column("recorded_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    ForeignKeyConstraint(["task_id", "revision"], ["task_revisions.task_id", "task_revisions.revision"]),
    CheckConstraint(
        "(status = 'extracted' AND result IS NOT NULL AND error_code IS NULL AND error_detail IS NULL) OR "
        "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL AND error_detail IS NOT NULL)",
        name="ck_source_extractions_outcome",
    ),
)
