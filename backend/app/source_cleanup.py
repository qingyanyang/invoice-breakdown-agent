"""Remove a single unreferenced source or workbook object under its task lock."""

import argparse
import asyncio
import re
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import DatabaseConfigurationError, create_application_engine
from app.source_storage import LocalSourceStorage, StorageError, create_output_storage, create_source_storage
from app.tasks import discard_unreferenced
from app.workbooks import discard_unreferenced_workbook

UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
OBJECT_NAME = rf"(?:{UUID_PATTERN}\.xlsx|(?P<staging>\.{UUID_PATTERN}\.[a-z0-9_]+\.staging))"
SOURCE_KEY = re.compile(rf"sources/(?P<task>{UUID_PATTERN})/{OBJECT_NAME}")
OUTPUT_KEY = re.compile(rf"outputs/(?P<task>{UUID_PATTERN})/{OBJECT_NAME}")


async def cleanup(key: str) -> bool:
    if match := SOURCE_KEY.fullmatch(key):
        storage = create_source_storage(settings)
        discard = discard_unreferenced
    elif match := OUTPUT_KEY.fullmatch(key):
        storage = create_output_storage(settings)
        discard = discard_unreferenced_workbook
    else:
        raise ValueError("Expected a server-generated source, workbook or staging key")
    if storage is None:
        raise StorageError("Storage unavailable")
    try:
        if match["staging"] and not isinstance(storage, LocalSourceStorage):
            raise StorageError("Staging cleanup requires the original filesystem storage")
        engine = create_application_engine(settings)
        try:
            return await discard(engine, storage, UUID(match["task"]), key)
        finally:
            await engine.dispose()
    finally:
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="Storage key reported in the application logs")
    args = parser.parse_args()
    try:
        deleted = asyncio.run(cleanup(args.key))
    except ValueError:
        parser.error("Expected a server-generated source, workbook or staging key")
    except (DatabaseConfigurationError, SQLAlchemyError, StorageError):
        parser.exit(1, "Cleanup could not be confirmed. Retry when the database and storage are available.\n")
    print("Unreferenced object removed or already absent." if deleted else "Object retained because it is referenced.")


if __name__ == "__main__":
    main()
