from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Integer, String, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

source_allocations = Table(
    "source_allocations",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("extractor_version", String(40), primary_key=True),
    Column("reconciliation_generation", Uuid, primary_key=True),
    Column("classification_id", Uuid, primary_key=True),
    Column("allocation_id", Uuid, nullable=False),
    Column("allocator_version", String(40), nullable=False),
    Column("input_sha256", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("operator", String(128), nullable=False),
    Column("policy", JSONB, nullable=False),
    Column("report", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "revision", "extractor_version", "reconciliation_generation"],
        [
            "source_classifications.task_id", "source_classifications.revision",
            "source_classifications.extractor_version", "source_classifications.reconciliation_generation",
        ],
    ),
    CheckConstraint("status IN ('blocked', 'ready_for_workbook')", name="ck_source_allocations_status"),
)
