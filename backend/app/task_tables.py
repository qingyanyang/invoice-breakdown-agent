from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Table, Uuid, func,
)

from app.database import metadata

tasks = Table(
    "tasks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("operation", String(16), nullable=False),
    Column("current_revision", Integer, nullable=False),
    CheckConstraint("current_revision >= 1", name="ck_tasks_current_revision"),
    CheckConstraint("operation = 'create'", name="ck_tasks_operation"),
)

task_revisions = Table(
    "task_revisions",
    metadata,
    Column("task_id", Uuid, ForeignKey("tasks.id"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("status", String(24), nullable=False),
    Column("source_key", String(110)),
    Column("source_filename", String(255)),
    Column("source_size", Integer),
    Column("source_sha256", String(64)),
    Column("target_amount", Numeric(18, 2)),
    Column("target_gst_basis", String(16)),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Index("ix_task_revisions_source_key", "source_key"),
    CheckConstraint("revision >= 1", name="ck_task_revisions_revision"),
    CheckConstraint(
        "(target_amount IS NULL AND target_gst_basis IS NULL) OR "
        "(target_amount IS NOT NULL AND target_amount >= 0 AND "
        "target_gst_basis IS NOT NULL AND target_gst_basis IN "
        "('excluding_gst', 'including_gst', 'unknown'))",
        name="ck_task_revisions_target",
    ),
    CheckConstraint(
        "(status = 'awaiting_source' AND source_key IS NULL AND source_filename IS NULL "
        "AND source_size IS NULL AND source_sha256 IS NULL) OR "
        "(status = 'source_uploaded' AND source_key IS NOT NULL AND source_filename IS NOT NULL "
        "AND source_size IS NOT NULL AND source_size > 0 AND source_sha256 IS NOT NULL)",
        name="ck_task_revisions_source",
    ),
)


task_rule_pins = Table(
    "task_rule_pins",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("primary_sha256", String(64), nullable=False),
    Column("primary_version", String(96), nullable=False),
    Column("supplementary_sha256", String(64)),
    Column("supplementary_version", String(96)),
    ForeignKeyConstraint(["task_id", "revision"], ["task_revisions.task_id", "task_revisions.revision"]),
    CheckConstraint(
        "(supplementary_sha256 IS NULL) = (supplementary_version IS NULL)",
        name="ck_task_rule_supplementary",
    ),
)
