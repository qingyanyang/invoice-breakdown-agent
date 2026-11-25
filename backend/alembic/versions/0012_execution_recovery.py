"""Persist the input context and storage write outcomes needed for safe recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0012_execution_recovery"
down_revision = "0011_execution_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.add_column("request_receipts", sa.Column("context_sha256", sa.String(64)), schema=schema)
    op.add_column(
        "execution_objects", sa.Column("write_state", sa.String(16), nullable=False, server_default="unconfirmed"),
        schema=schema,
    )
    op.create_check_constraint(
        "ck_execution_object_write_state", "execution_objects",
        "write_state IN ('pending', 'written', 'collision', 'unconfirmed')", schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_constraint("ck_execution_object_write_state", "execution_objects", type_="check", schema=schema)
    op.drop_column("execution_objects", "write_state", schema=schema)
    op.drop_column("request_receipts", "context_sha256", schema=schema)
