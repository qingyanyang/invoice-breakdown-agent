from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Integer, String, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

source_classifications = Table(
    "source_classifications",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("extractor_version", String(40), primary_key=True),
    Column("reconciliation_generation", Uuid, primary_key=True),
    Column("source_sha256", String(64), nullable=False),
    Column("classifier_version", String(40), nullable=False),
    Column("proposal_id", Uuid, nullable=False),
    Column("status", String(32), nullable=False),
    Column("skill_sha256", String(64)),
    Column("model", String(255)),
    Column("proposal", JSONB),
    Column("decisions", JSONB, nullable=False),
    Column("report", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "revision", "extractor_version"],
        ["source_reconciliations.task_id", "source_reconciliations.revision", "source_reconciliations.extractor_version"],
    ),
    CheckConstraint(
        "status IN ('failed', 'blocked', 'ready_for_allocation')", name="ck_source_classifications_status",
    ),
)
