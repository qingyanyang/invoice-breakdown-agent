from psycopg import sql
from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.config import settings

metadata = MetaData(schema=settings.postgres_schema)

if not settings.database_url:
    raise RuntimeError("DATABASE_URL is missing")

try:
    database_url = make_url(settings.database_url)
except (ArgumentError, ValueError):
    raise RuntimeError("DATABASE_URL is not a valid PostgreSQL URL") from None

if database_url.drivername in {"postgres", "postgresql"}:
    database_url = database_url.set(drivername="postgresql+psycopg")
if database_url.drivername != "postgresql+psycopg":
    raise RuntimeError("DATABASE_URL must use PostgreSQL with psycopg")

engine = create_engine(database_url, pool_pre_ping=True)


@event.listens_for(engine, "connect")
def set_search_path(dbapi_connection, _connection_record) -> None:
    # Apply the session setting outside any transaction so rollback cannot undo it.
    previous_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SET SESSION search_path TO {}").format(
                    sql.Identifier(settings.postgres_schema)
                )
            )
    finally:
        dbapi_connection.autocommit = previous_autocommit


def check_database() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
