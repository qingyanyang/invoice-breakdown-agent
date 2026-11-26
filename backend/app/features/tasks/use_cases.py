import logging
from hashlib import sha256
from uuid import UUID, uuid4

from anyio import CancelScope, current_time, fail_after, to_thread
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.outcomes import FailureKind, FileContent, UseCaseError
from app.features.tasks.models import Target, CreateTask, UpdateTarget, SourceDetails, TaskDetails
from app.config import settings
from app.database import database_for
from app.delivery_guard import guard_delivery_inputs
from app.execution import (
    ExecutionFailure, acquire, claim, executing, fence, object_may_be_discarded,
    register_object, settle, transaction, write_registered_object,
)
from app.request_receipts import ReceiptKey, reserve_receipt, save_receipt
from app.source_storage import (
    SourceAlreadyExists, SourceIntegrityError, SourceStorage, StorageError, read_verified_source,
)
from app.features.tasks.tables import task_revisions, tasks
from app.features.tasks.upload import XLSX_MEDIA_TYPE, read_upload, validate_filename, validate_workbook

logger = logging.getLogger(__name__)


def storage_for(resources) -> SourceStorage:
    storage = resources.source_storage
    if storage is None:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Source storage unavailable")
    return storage


def target_fields(target: Target | None) -> dict:
    return {
        "target_amount": target.amount if target else None,
        "target_gst_basis": target.gst_basis if target else None,
    }


def task_details(row) -> TaskDetails:
    return TaskDetails(
        id=row["task_id"],
        operation=row["operation"],
        status=row["status"],
        revision=row["revision"],
        target=(
            Target(amount=row["target_amount"], gst_basis=row["target_gst_basis"])
            if row["target_amount"] is not None else None
        ),
        source=(
            SourceDetails(
                filename=row["source_filename"], size=row["source_size"], sha256=row["source_sha256"]
            )
            if row["source_key"] is not None else None
        ),
    )


async def owned_revision(
    connection: AsyncConnection, task_id: UUID, owner: str, *, revision: int | None = None,
):
    selected_revision = tasks.c.current_revision if revision is None else revision
    result = await connection.execute(
        select(tasks.c.operation, task_revisions)
        .join_from(tasks, task_revisions, tasks.c.id == task_revisions.c.task_id)
        .where(tasks.c.id == task_id, tasks.c.owner == owner,
               task_revisions.c.revision == selected_revision)
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise UseCaseError(FailureKind.NOT_FOUND, "Task or revision not found")
    return row


async def reserve_owned_receipt(connection, task_id, key):
    if key is None:
        return None
    await owned_revision(connection, task_id, key.operator)
    return await reserve_receipt(connection, key)


async def lock_current_revision(
    connection: AsyncConnection, task_id: UUID, owner: str, expected_revision: int,
):
    current = await connection.scalar(
        select(tasks.c.current_revision)
        .where(tasks.c.id == task_id, tasks.c.owner == owner)
        .with_for_update()
    )
    if current is None:
        raise UseCaseError(FailureKind.NOT_FOUND, "Task or revision not found")
    if current != expected_revision:
        raise UseCaseError(FailureKind.CONFLICT, "Task revision has changed")
    return await owned_revision(connection, task_id, owner, revision=current)


async def append_revision(connection: AsyncConnection, current, changes: dict) -> TaskDetails:
    await guard_delivery_inputs(connection, current["task_id"])
    next_revision = dict(current)
    operation = next_revision.pop("operation")
    next_revision.pop("created_at")
    next_revision.update(changes, revision=current["revision"] + 1)
    await connection.execute(insert(task_revisions).values(next_revision))
    await connection.execute(
        update(tasks).where(tasks.c.id == current["task_id"])
        .values(current_revision=next_revision["revision"])
    )
    return task_details(dict(next_revision, operation=operation))


async def discard_unreferenced(
    engine: AsyncEngine, storage: SourceStorage, task_id: UUID, key: str,
) -> bool:
    # After a disconnect on commit, reconnect before deciding whether the file is safe to delete.
    async with engine.begin() as connection:
        locked_task = await connection.scalar(
            select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
        )
        if locked_task is None:
            raise StorageError("Source task could not be verified")
        referenced = await connection.scalar(
            select(task_revisions.c.task_id).where(task_revisions.c.source_key == key).limit(1)
        )
        if referenced is not None or not await object_may_be_discarded(connection, key):
            return False
    await to_thread.run_sync(storage.delete, key)
    return True


async def create_task(
    payload: CreateTask, resources, operator: str, idempotency_key: str | None = None,
) -> TaskDetails:
    key = ReceiptKey.from_request(operator, "create_task", idempotency_key, payload)
    async with transaction(database_for(resources)) as connection:
        receipt = await reserve_receipt(connection, key)
        if receipt is not None:
            return TaskDetails.model_validate(receipt)
        task_id = uuid4()
        await connection.execute(
            insert(tasks).values(id=task_id, owner=operator,
                                 operation=payload.operation, current_revision=1)
        )
        await connection.execute(
            insert(task_revisions).values(task_id=task_id, revision=1, status="awaiting_source",
                                          **target_fields(payload.target))
        )
        result = task_details(await owned_revision(connection, task_id, operator))
        return await save_receipt(connection, key, result)


async def read_task(task_id: UUID, resources, operator: str) -> TaskDetails:
    async with transaction(database_for(resources)) as connection:
        return task_details(await owned_revision(connection, task_id, operator))


async def change_target(
    task_id: UUID, payload: UpdateTarget, resources, operator: str, idempotency_key: str | None = None,
) -> TaskDetails:
    key = ReceiptKey.from_request(operator, "change_target", idempotency_key, [task_id, payload])
    async with transaction(database_for(resources)) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return TaskDetails.model_validate(receipt)
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        changes = target_fields(payload.target)
        if all(current[field] == value for field, value in changes.items()):
            result = task_details(current)
        else:
            result = await append_revision(connection, current, changes)
        return await save_receipt(connection, key, result)


async def upload_source(
    task_id: UUID, resources, operator: str,
    expected_revision: int, filename: str, idempotency_key: str | None = None, *, chunks,
) -> TaskDetails:
    engine = database_for(resources)
    async with transaction(engine) as connection:
        current = await owned_revision(connection, task_id, operator)
        if idempotency_key is None and current["revision"] != expected_revision:
            raise UseCaseError(FailureKind.CONFLICT, "Task revision has changed")

    validate_filename(filename)
    try:
        with fail_after(settings.execution_upload_seconds) as limit:
            data = await read_upload(chunks)
            await resources.upload_validator.run(validate_workbook, data)
            if current_time() >= limit.deadline:
                raise TimeoutError
    except TimeoutError:
        raise UseCaseError(FailureKind.TIMED_OUT, "Source upload exceeded its time limit") from None
    checksum = sha256(data).hexdigest()
    material = [task_id, expected_revision, filename, checksum, len(data)]
    receipt_key = ReceiptKey.from_request(operator, "upload_source", idempotency_key, material)

    async def prepare(connection):
        receipt = await reserve_receipt(connection, receipt_key)
        if receipt is not None:
            return TaskDetails.model_validate(receipt), None
        current = await lock_current_revision(connection, task_id, operator, expected_revision)
        attempt = await claim(connection, current, operator, "upload_source", material, settings.execution_upload_seconds)
        return None, attempt

    receipt, attempt = await acquire(engine, prepare)
    if attempt is None:
        return receipt

    key = f"sources/{task_id}/{uuid4()}.xlsx"
    storage_attempted = False
    storage = None
    try:
        async with executing(engine, attempt):
            storage = storage_for(resources)
            await register_object(engine, attempt, key)
            storage_attempted = True
            try:
                await write_registered_object(engine, attempt, resources.storage_worker, storage, key, data)
            except SourceAlreadyExists:
                storage_attempted = False
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Source storage unavailable", "storage_collision", retryable=True) from None
            except StorageError:
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Source storage unavailable", "source_storage_unavailable", retryable=True) from None

            async with transaction(engine) as connection:
                await reserve_receipt(connection, receipt_key)
                current = await lock_current_revision(connection, task_id, operator, expected_revision)
                await fence(connection, attempt)
                result = await append_revision(connection, current, {
                    "status": "source_uploaded", "source_key": key, "source_filename": filename,
                    "source_size": len(data), "source_sha256": checksum,
                })
                await save_receipt(connection, receipt_key, result)
                await settle(connection, attempt, "succeeded", result={"revision": result.revision, "source_sha256": checksum})
                return result
    except BaseException:
        if storage_attempted and key in resources.storage_worker.writes:
            logger.warning("Source cleanup pending for key %s", key)
        elif storage_attempted:
            try:
                with CancelScope(shield=True):
                    await discard_unreferenced(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("Source cleanup pending for key %s", key)
        raise


async def read_source(
    task_id: UUID, resources, operator: str,
    revision: int | None = None,
) -> FileContent:
    async with transaction(database_for(resources)) as connection:
        row = await owned_revision(connection, task_id, operator, revision=revision)
    if row["source_key"] is None:
        raise UseCaseError(FailureKind.NOT_FOUND, "Source not found")
    try:
        data = await to_thread.run_sync(
            read_verified_source, storage_for(resources), row["source_key"],
            row["source_size"], row["source_sha256"],
        )
    except SourceIntegrityError:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Source integrity check failed") from None
    except StorageError:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Source storage unavailable") from None
    return FileContent(data, row["source_filename"], XLSX_MEDIA_TYPE)
