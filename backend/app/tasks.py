import logging
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from anyio import CancelScope, current_time, fail_after, to_thread
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import settings
from app.execution import (
    ExecutionFailure, acquire, claim, executing, fence, object_may_be_discarded,
    register_object, settle, transaction, write_registered_object,
)
from app.operators import Operator
from app.request_receipts import ReceiptKey, RequestKey, reserve_receipt, save_receipt
from app.source_storage import (
    SourceAlreadyExists, SourceIntegrityError, SourceStorage, StorageError, read_verified_source,
)
from app.task_tables import task_revisions, tasks
from app.workbook_upload import XLSX_MEDIA_TYPE, read_upload, validate_filename, validate_workbook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])
GSTBasis = Literal["excluding_gst", "including_gst", "unknown"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    gst_basis: GSTBasis


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create"] = "create"
    target: Target | None = None


class UpdateTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    target: Target | None


class SourceDetails(BaseModel):
    filename: str
    size: int
    sha256: str


class TaskDetails(BaseModel):
    id: UUID
    operation: Literal["create"]
    status: Literal["awaiting_source", "source_uploaded"]
    revision: int
    target: Target | None
    source: SourceDetails | None


def database_for(request: Request) -> AsyncEngine:
    engine = request.app.state.database_engine
    if engine is None:
        raise HTTPException(503, "Task database unavailable")
    return engine


def storage_for(request: Request) -> SourceStorage:
    storage = request.app.state.source_storage
    if storage is None:
        raise HTTPException(503, "Source storage unavailable")
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
        raise HTTPException(404, "Task or revision not found")
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
        raise HTTPException(404, "Task or revision not found")
    if current != expected_revision:
        raise HTTPException(409, "Task revision has changed")
    return await owned_revision(connection, task_id, owner, revision=current)


async def append_revision(connection: AsyncConnection, current, changes: dict) -> TaskDetails:
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


@router.post("", status_code=201)
async def create_task(
    payload: CreateTask, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> TaskDetails:
    key = ReceiptKey.from_request(operator, "create_task", idempotency_key, payload)
    async with transaction(database_for(request)) as connection:
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


@router.get("/{task_id}")
async def read_task(task_id: UUID, request: Request, operator: Operator) -> TaskDetails:
    async with transaction(database_for(request)) as connection:
        return task_details(await owned_revision(connection, task_id, operator))


@router.put("/{task_id}/target")
async def change_target(
    task_id: UUID, payload: UpdateTarget, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> TaskDetails:
    key = ReceiptKey.from_request(operator, "change_target", idempotency_key, [task_id, payload])
    async with transaction(database_for(request)) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return TaskDetails.model_validate(receipt)
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        changes = target_fields(payload.target)
        if all(current[field] == value for field, value in changes.items()):
            result = task_details(current)
        else:
            result = await append_revision(connection, current, changes)
        return await save_receipt(connection, key, result)


@router.put(
    "/{task_id}/source",
    openapi_extra={"requestBody": {"required": True, "content": {
        XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
    }}},
)
async def upload_source(
    task_id: UUID, request: Request, operator: Operator,
    expected_revision: Annotated[int, Query(ge=1)], filename: str, idempotency_key: RequestKey = None,
) -> TaskDetails:
    engine = database_for(request)
    async with transaction(engine) as connection:
        current = await owned_revision(connection, task_id, operator)
        if idempotency_key is None and current["revision"] != expected_revision:
            raise HTTPException(409, "Task revision has changed")

    validate_filename(filename)
    try:
        with fail_after(settings.execution_upload_seconds) as limit:
            data = await read_upload(request)
            await request.app.state.upload_validator.run(validate_workbook, data)
            if current_time() >= limit.deadline:
                raise TimeoutError
    except TimeoutError:
        raise HTTPException(504, "Source upload exceeded its time limit") from None
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
            storage = storage_for(request)
            await register_object(engine, attempt, key)
            storage_attempted = True
            try:
                await write_registered_object(engine, attempt, request.app.state.storage_worker, storage, key, data)
            except SourceAlreadyExists:
                storage_attempted = False
                raise ExecutionFailure(503, "Source storage unavailable", "storage_collision", retryable=True) from None
            except StorageError:
                raise ExecutionFailure(503, "Source storage unavailable", "source_storage_unavailable", retryable=True) from None

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
        if storage_attempted and key in request.app.state.storage_worker.writes:
            logger.warning("Source cleanup pending for key %s", key)
        elif storage_attempted:
            try:
                with CancelScope(shield=True):
                    await discard_unreferenced(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("Source cleanup pending for key %s", key)
        raise


@router.get("/{task_id}/source", response_class=Response)
async def read_source(
    task_id: UUID, request: Request, operator: Operator,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    async with transaction(database_for(request)) as connection:
        row = await owned_revision(connection, task_id, operator, revision=revision)
    if row["source_key"] is None:
        raise HTTPException(404, "Source not found")
    try:
        data = await to_thread.run_sync(
            read_verified_source, storage_for(request), row["source_key"],
            row["source_size"], row["source_sha256"],
        )
    except SourceIntegrityError:
        raise HTTPException(503, "Source integrity check failed") from None
    except StorageError:
        raise HTTPException(503, "Source storage unavailable") from None
    return Response(
        data, media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["source_filename"], safe=""),
            "Cache-Control": "private, no-store",
        },
    )
