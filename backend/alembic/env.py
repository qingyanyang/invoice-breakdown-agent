from alembic import context
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

# Register application tables in the shared metadata.
from app import execution_tables
from app.features.allocation import tables as allocation_tables
from app.features.classification import tables as classification_tables
from app.features.extraction import tables as extraction_tables
from app.features.qa import tables as qa_tables
from app.features.reconciliation import tables as reconciliation_tables
from app.features.tasks import tables as task_tables
from app.features.workbooks import tables as workbook_tables
from app.workflows import tables as workflow_tables
from app.config import settings
from app.database import create_database_engine, metadata
from app.channels.slack import tables as slack_tables

schema = settings.postgres_schema


def run_migrations(engine: Engine) -> None:
    if context.is_offline_mode():
        context.configure(
            dialect_name=engine.dialect.name,
            target_metadata=metadata,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.execute(CreateSchema(schema, if_not_exists=True))
            quoted_schema = engine.dialect.identifier_preparer.quote_identifier(schema)
            context.execute(f"SET search_path TO {quoted_schema}")
            context.run_migrations()
        return

    # Create and commit the schema before Alembic initializes its version table.
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema, if_not_exists=True))
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.run_migrations()


engine = create_database_engine(settings)
try:
    run_migrations(engine)
finally:
    engine.dispose()
