"""Persist Slack file completion backoff for definite rate-limit responses."""

import sqlalchemy as sa
from alembic import op

revision = "0015_slack_share_retry"
down_revision = "0014_slack_dm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.add_column("slack_files", sa.Column("retry_at", sa.DateTime(timezone=True)), schema=schema)


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_column("slack_files", "retry_at", schema=schema)
