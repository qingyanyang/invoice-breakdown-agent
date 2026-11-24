"""Record the database baseline before application tables are introduced."""

revision = "0001_database_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The scaffold has no application tables."""


def downgrade() -> None:
    """The baseline created no application tables to remove."""
