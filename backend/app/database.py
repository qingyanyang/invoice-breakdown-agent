from sqlalchemy import create_engine, text

from app.config import settings

if not settings.database_url:
    raise RuntimeError("DATABASE_URL is missing")

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
)


def check_database() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
