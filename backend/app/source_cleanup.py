"""Remove a single unreferenced source object after verifying the database."""

import argparse
import asyncio
import re
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import DatabaseConfigurationError, create_application_engine
from app.source_storage import StorageError, create_source_storage
from app.tasks import discard_unreferenced

UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


async def cleanup(task_id: UUID, key: str) -> bool:
    storage = create_source_storage(settings)
    if storage is None:
        raise StorageError("Source storage unavailable")
    try:
        engine = create_application_engine(settings)
        try:
            return await discard_unreferenced(engine, storage, task_id, key)
        finally:
            await engine.dispose()
    finally:
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="Source storage key reported in the application logs")
    args = parser.parse_args()
    match = re.fullmatch(rf"sources/({UUID_PATTERN})/{UUID_PATTERN}\.xlsx", args.key)
    if match is None:
        parser.error("Expected a server-generated sources/<task-id>/<file-id>.xlsx key")
    try:
        deleted = asyncio.run(cleanup(UUID(match[1]), args.key))
    except (DatabaseConfigurationError, SQLAlchemyError, StorageError):
        parser.exit(1, "Cleanup could not be confirmed. Retry when the database and storage are available.\n")
    print("Unreferenced source removed or already absent." if deleted else "Source retained: referenced by a task revision.")


if __name__ == "__main__":
    main()
