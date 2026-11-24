from alembic import context
from sqlalchemy.schema import CreateSchema

from app.config import settings
from app.database import engine, metadata

schema = settings.postgres_schema


def run_migrations() -> None:
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


run_migrations()
