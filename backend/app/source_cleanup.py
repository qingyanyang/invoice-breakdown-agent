"""Remove an unreferenced task object after checking execution and manifest ownership."""

import argparse
import asyncio
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import DatabaseConfigurationError, create_application_engine
from app.source_storage import LocalSourceStorage, StorageError, create_output_storage, create_source_storage
from app.delivery import discard_unreferenced_qa
from app.execution_tables import execution_objects, execution_runs
from app.tasks import discard_unreferenced
from app.workbooks import discard_unreferenced_workbook

UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
OBJECT_NAME = rf"(?:{UUID_PATTERN}\.xlsx|(?P<staging>\.{UUID_PATTERN}\.[a-z0-9_]+\.staging))"
SOURCE_KEY = re.compile(rf"sources/(?P<task>{UUID_PATTERN})/{OBJECT_NAME}")
OUTPUT_KEY = re.compile(rf"outputs/(?P<task>{UUID_PATTERN})/{OBJECT_NAME}")
QA_KEY = re.compile(
    rf"qa/(?P<task>{UUID_PATTERN})/{UUID_PATTERN}/"
    r"(?:candidate\.xlsx|preview\.pdf|(?P<staging>\.(?:candidate|preview)\.[a-z0-9_]+\.staging))",
)


async def cleanup(key: str) -> bool:
    if match := SOURCE_KEY.fullmatch(key):
        storage = create_source_storage(settings)
        discard = discard_unreferenced
    elif match := OUTPUT_KEY.fullmatch(key):
        storage = create_output_storage(settings)
        discard = discard_unreferenced_workbook
    elif match := QA_KEY.fullmatch(key):
        storage = create_output_storage(settings)
        discard = discard_unreferenced_qa
    else:
        raise ValueError("Expected a server-generated source, workbook, QA or staging key")
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


async def cleanup_execution(execution_id: UUID) -> tuple[int, int]:
    engine = create_application_engine(settings)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(select(execution_runs.c.id).where(execution_runs.c.id == execution_id))
            if exists is None:
                raise ValueError("Execution not found")
            keys = (await connection.scalars(select(execution_objects.c.storage_key).where(
                execution_objects.c.execution_id == execution_id,
            ).order_by(execution_objects.c.attempt))).all()
    finally:
        await engine.dispose()
    deleted = 0
    for key in keys:
        if await cleanup(key):
            deleted += 1
    return deleted, len(keys) - deleted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", nargs="?", help="Storage key reported in the application logs")
    parser.add_argument("--execution-id", type=UUID, help="Check every storage key registered by this execution")
    args = parser.parse_args()
    if bool(args.key) == bool(args.execution_id):
        parser.error("Provide a storage key or one execution ID")
    try:
        if args.execution_id:
            deleted, retained = asyncio.run(cleanup_execution(args.execution_id))
            print(f"{deleted} unreferenced objects removed or already absent. {retained} objects retained.")
            return
        deleted = asyncio.run(cleanup(args.key))
    except ValueError as error:
        parser.error(str(error))
    except (DatabaseConfigurationError, SQLAlchemyError, StorageError):
        parser.exit(1, "Cleanup could not be confirmed. Retry when the database and storage are available.\n")
    if deleted:
        print("Unreferenced object removed or already absent.")
    else:
        print("Object retained because it is referenced, belongs to an active attempt, or lacks write ownership evidence.")


if __name__ == "__main__":
    main()
