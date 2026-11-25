"""Durable identities for mutations and workflow attempts."""

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Table,
    UniqueConstraint, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata

request_receipts = Table(
    "request_receipts",
    metadata,
    Column("operator", String(128), primary_key=True),
    Column("operation", String(40), primary_key=True),
    Column("key_sha256", String(64), primary_key=True),
    Column("payload_sha256", String(64), nullable=False),
    Column("response", JSONB(none_as_null=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


execution_runs = Table(
    "execution_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("operator", String(128), nullable=False),
    Column("operation", String(40), nullable=False),
    Column("task_id", Uuid, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("input_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("worker_token", Uuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt_expires_at", DateTime(timezone=True)),
    Column("deadline", DateTime(timezone=True), nullable=False),
    Column("retry_at", DateTime(timezone=True)),
    Column("failure_code", String(60)),
    Column("exhaustion_reason", String(24)),
    Column("result", JSONB(none_as_null=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["task_id", "revision"], ["task_revisions.task_id", "task_revisions.revision"]),
    UniqueConstraint("operator", "operation", "task_id", "revision", "input_sha256", name="uq_execution_inputs"),
    Index("ix_execution_task", "task_id", "revision", "updated_at"),
    CheckConstraint("attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts", name="ck_execution_attempt"),
    CheckConstraint(
        "status IN ('running', 'retryable', 'succeeded', 'blocked', 'failed', 'exhausted')",
        name="ck_execution_status",
    ),
)

execution_objects = Table(
    "execution_objects",
    metadata,
    Column("storage_key", String(150), primary_key=True),
    Column("execution_id", Uuid, ForeignKey("execution_runs.id"), nullable=False),
    Column("attempt", Integer, nullable=False),
)
