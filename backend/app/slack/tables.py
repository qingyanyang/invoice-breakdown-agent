"""Durable inbox, chat task links, and outbound file receipts."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Table, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB

from app.database import metadata


inbox = Table(
    "slack_inbox", metadata,
    Column("key", String(64), primary_key=True),
    Column("event", JSONB, nullable=False),
    Column("state", String(20), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("error", String(64)),
    Index("ix_slack_inbox_available", "state", "available_at"),
)


threads = Table(
    "slack_threads", metadata,
    Column("key", String(64), primary_key=True),
    Column("binding", JSONB, nullable=False),
    Column("task_id", Uuid, ForeignKey("tasks.id"), nullable=False, unique=True),
    Column("revision", Integer, nullable=False),
)


controls = Table(
    "slack_controls", metadata,
    Column("id", String(64), primary_key=True),
    Column("thread_key", String(64), ForeignKey("slack_threads.key"), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


visual_votes = Table(
    "slack_visual_votes", metadata,
    Column("thread_key", String(64), ForeignKey("slack_threads.key"), primary_key=True),
    Column("qa_id", Uuid, primary_key=True),
    Column("sheet", String(31), primary_key=True),
    Column("outcome", String(8), nullable=False),
)


files = Table(
    "slack_files", metadata,
    Column("key", String(64), primary_key=True),
    Column("thread_key", String(64), ForeignKey("slack_threads.key"), nullable=False),
    Column("file_id", String(64), nullable=False),
    Column("state", String(20), nullable=False),
    Column("retry_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
