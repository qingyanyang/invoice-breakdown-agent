import asyncio
import re
from contextvars import ContextVar
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg import AsyncConnection, sql, waiting
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings, settings
from app.outcomes import FailureKind, UseCaseError

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
CONNECT_TIMEOUT_SECONDS = 3
POOL_TIMEOUT_SECONDS = 2
READINESS_STATEMENT_TIMEOUT_MS = 2_000
READINESS_TIMEOUT_SECONDS = 4
POOL_OPTIONS = {"pool_pre_ping": True, "pool_timeout": POOL_TIMEOUT_SECONDS}

readiness_deadline: ContextVar[float | None] = ContextVar("readiness_deadline", default=None)

metadata = MetaData(schema=settings.postgres_schema)


class DatabaseConfigurationError(RuntimeError):
    """A database setting is unusable. Its value is safe to omit from diagnostics."""


def database_for(resources) -> AsyncEngine:
    engine = resources.database_engine
    if engine is None:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Task database unavailable")
    return engine


def database_url(config: Settings) -> URL:
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

    return url


class ReadinessConnection(AsyncConnection):
    async def wait(self, gen, interval=0.1, timeout=None):
        deadline = readiness_deadline.get()
        if deadline is None:
            return await super().wait(gen, interval=interval, timeout=timeout)

        # Pool cleanup can be shielded from cancellation. Give its driver waits
        # the same deadline and close the socket without a server round trip.
        try:
            async with asyncio.timeout_at(deadline):
                return await waiting.wait_async(
                    gen, self.pgconn.socket, interval=interval, timeout=timeout
                )
        except (TimeoutError, asyncio.CancelledError):
            await self.close()
            raise


def configure_search_path(engine: Engine, schema: str) -> None:
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


def create_database_engine(config: Settings) -> Engine:
    url = database_url(config)
    try:
        engine = create_engine(
            url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}, **POOL_OPTIONS
        )
    except ArgumentError:
        raise DatabaseConfigurationError("DATABASE_URL has invalid connection settings") from None
    configure_search_path(engine, config.postgres_schema)
    return engine


def create_application_engine(config: Settings) -> AsyncEngine:
    url = database_url(config)

    async def connect():
        args, kwargs = engine.dialect.create_connect_args(url)
        kwargs["connect_timeout"] = CONNECT_TIMEOUT_SECONDS
        return await ReadinessConnection.connect(*args, **kwargs)

    try:
        engine = create_async_engine(url, async_creator=connect, **POOL_OPTIONS)
    except ArgumentError:
        raise DatabaseConfigurationError("DATABASE_URL has invalid connection settings") from None
    configure_search_path(engine.sync_engine, config.postgres_schema)
    return engine


@cache
def repository_heads() -> frozenset[str]:
    migration_scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return frozenset(migration_scripts.get_heads())


async def database_heads(engine: AsyncEngine, schema: str) -> frozenset[str]:
    deadline = asyncio.get_running_loop().time() + READINESS_TIMEOUT_SECONDS
    token = readiness_deadline.set(deadline)
    try:
        async with asyncio.timeout_at(deadline), engine.begin() as connection:
            await connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {READINESS_STATEMENT_TIMEOUT_MS}"
            )

            def current_heads(sync_connection):
                migration = MigrationContext.configure(
                    connection=sync_connection,
                    opts={"version_table_schema": schema},
                )
                return frozenset(migration.get_current_heads())

            return await connection.run_sync(current_heads)
    finally:
        readiness_deadline.reset(token)
