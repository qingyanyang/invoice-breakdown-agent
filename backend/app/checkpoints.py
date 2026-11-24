"""Open LangGraph's PostgreSQL checkpointer and install its tables explicitly."""

import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, Error, sql
from psycopg.rows import dict_row
from sqlalchemy.exc import ArgumentError

from app.config import Settings, settings
from app.database import CONNECT_TIMEOUT_SECONDS, DatabaseConfigurationError, database_url


@asynccontextmanager
async def open_checkpointer(config: Settings):
    url = database_url(config)
    try:
        args, kwargs = url.get_dialect()().create_connect_args(url)
    except ArgumentError:
        raise DatabaseConfigurationError("DATABASE_URL has invalid connection settings") from None
    kwargs["connect_timeout"] = CONNECT_TIMEOUT_SECONDS
    async with await AsyncConnection.connect(
        *args, **kwargs, autocommit=True, prepare_threshold=0, row_factory=dict_row,
    ) as connection:
        await connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(config.postgres_schema))
        )
        yield AsyncPostgresSaver(connection)


async def setup_checkpoints(config: Settings) -> None:
    async with open_checkpointer(config) as checkpointer:
        await checkpointer.setup()


def main() -> None:
    try:
        asyncio.run(setup_checkpoints(settings))
    except (Error, DatabaseConfigurationError, TimeoutError):
        raise SystemExit("Checkpoint setup unavailable. Check database settings and run Alembic first.") from None
    print("LangGraph checkpoint tables are ready.")


if __name__ == "__main__":
    main()
