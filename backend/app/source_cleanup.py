"""Remove a single unreferenced source object after verifying the database."""

import argparse
import asyncio
import re
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import DatabaseConfigurationError, create_application_engine
from app.source_storage import LocalSourceStorage, StorageError, create_source_storage
from app.tasks import discard_unreferenced

UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SOURCE_KEY = re.compile(
    rf"sources/(?P<task>{UUID_PATTERN})/"
    rf"(?:{UUID_PATTERN}\.xlsx|(?P<staging>\.{UUID_PATTERN}\.[a-z0-9_]+\.staging))"
)


async def cleanup(key: str) -> bool:
    match = SOURCE_KEY.fullmatch(key)
    if match is None:
        raise ValueError("Expected a server-generated source or staging key")
    storage = create_source_storage(settings)
    if storage is None:
        raise StorageError("Source storage unavailable")
    try:
        if match["staging"] and not isinstance(storage, LocalSourceStorage):
            raise StorageError("Staging cleanup requires the original filesystem storage")
        engine = create_application_engine(settings)
        try:
            return await discard_unreferenced(engine, storage, UUID(match['task']), key)
        finally:
            await engine.dispose()
    finally:
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="Source storage key reported in the application logs")
    args = parser.parse_args()
    try:
        deleted = asyncio.run(cleanup(args.key))
    except ValueError:
        parser.error("Expected a server-generated source or staging key")
    except (DatabaseConfigurationError, SQLAlchemyError, StorageError):
        parser.exit(1, "Cleanup could not be confirmed. Retry when the database and storage are available.\n")
    print("Unreferenced source removed or already absent." if deleted else "Source retained: referenced by a task revision.")


if __name__ == "__main__":
    main()
