import re
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError

from app.config import Settings, settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
CONNECT_TIMEOUT_SECONDS = 3
POOL_TIMEOUT_SECONDS = 2
READINESS_STATEMENT_TIMEOUT_MS = 2_000

metadata = MetaData(schema=settings.postgres_schema)


class DatabaseConfigurationError(RuntimeError):
    """A database setting is unusable. Its value is safe to omit from diagnostics."""


def create_database_engine(config: Settings) -> Engine:
    if not config.database_url:
        raise DatabaseConfigurationError("DATABASE_URL is missing")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", config.postgres_schema):
        raise DatabaseConfigurationError("POSTGRES_SCHEMA is not a valid schema name")

    try:
        url = make_url(config.database_url)
    except (ArgumentError, ValueError):
        raise DatabaseConfigurationError("DATABASE_URL is not a valid PostgreSQL URL") from None

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise DatabaseConfigurationError("DATABASE_URL must use PostgreSQL with psycopg")

    try:
        engine = create_engine(
            url,
            connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            pool_pre_ping=True,
            pool_timeout=POOL_TIMEOUT_SECONDS,
        )
    except ArgumentError:
        raise DatabaseConfigurationError("DATABASE_URL has invalid connection settings") from None

    schema = config.postgres_schema

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, _connection_record) -> None:
        # Apply it outside a transaction so rollback cannot undo the session setting.
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET SESSION search_path TO {}").format(sql.Identifier(schema))
                )
        finally:
            dbapi_connection.autocommit = previous_autocommit

    return engine


@cache
def repository_heads() -> frozenset[str]:
    migration_scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return frozenset(migration_scripts.get_heads())


def database_heads(engine: Engine, schema: str) -> frozenset[str]:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"SET LOCAL statement_timeout = {READINESS_STATEMENT_TIMEOUT_MS}"
        )
        migration = MigrationContext.configure(
            connection=connection,
            opts={"version_table_schema": schema},
        )
        return frozenset(migration.get_current_heads())
