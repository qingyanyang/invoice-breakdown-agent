from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, String, Table, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

workbook_artifacts = Table(
    "workbook_artifacts",
    metadata,
    Column("artifact_id", Uuid, primary_key=True),
    Column("task_id", Uuid, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("source_sha256", String(64), nullable=False),
    Column("allocation_id", Uuid, nullable=False),
    Column("generator_version", String(40), nullable=False),
    Column("template_version", String(40), nullable=False),
    Column("version", Integer, nullable=False),
    Column("storage_key", String(110), nullable=False, unique=True),
    Column("filename", String(255), nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("report", JSONB, nullable=False),
    ForeignKeyConstraint(["task_id", "revision"], ["task_revisions.task_id", "task_revisions.revision"]),
    UniqueConstraint("task_id", "version", name="uq_workbook_artifact_version"),
    Index(
        "ix_workbook_artifact_inputs",
        "task_id", "revision", "source_sha256", "allocation_id", "generator_version", "template_version", "version",
    ),
    CheckConstraint("version > 0 AND size > 0", name="ck_workbook_artifact_size_version"),
)
