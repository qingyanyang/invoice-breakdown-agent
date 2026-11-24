import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from anyio import CancelScope, to_thread
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.operators import Operator
from app.source_storage import SourceAlreadyExists, SourceStorage, StorageError
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


@asynccontextmanager
async def transaction(engine: AsyncEngine):
    try:
        async with engine.begin() as connection:
            yield connection
    except SQLAlchemyError as error:
        logger.warning("Task database operation failed: %s", type(error).__name__)
        raise HTTPException(503, "Task database unavailable") from None


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
        if referenced is not None:
            return False
        await to_thread.run_sync(storage.delete, key)
        return True


@router.post("", status_code=201)
async def create_task(payload: CreateTask, request: Request, operator: Operator) -> TaskDetails:
    task_id = uuid4()
    async with transaction(database_for(request)) as connection:
        await connection.execute(
            insert(tasks).values(id=task_id, owner=operator,
                                 operation=payload.operation, current_revision=1)
        )
        await connection.execute(
            insert(task_revisions).values(task_id=task_id, revision=1, status="awaiting_source",
                                          **target_fields(payload.target))
        )
        return task_details(await owned_revision(connection, task_id, operator))


@router.get("/{task_id}")
async def read_task(task_id: UUID, request: Request, operator: Operator) -> TaskDetails:
    async with transaction(database_for(request)) as connection:
        return task_details(await owned_revision(connection, task_id, operator))


@router.put("/{task_id}/target")
async def change_target(
    task_id: UUID, payload: UpdateTarget, request: Request, operator: Operator,
) -> TaskDetails:
    async with transaction(database_for(request)) as connection:
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        changes = target_fields(payload.target)
        if all(current[field] == value for field, value in changes.items()):
            return task_details(current)
        return await append_revision(connection, current, changes)


@router.put(
    "/{task_id}/source",
    openapi_extra={"requestBody": {"required": True, "content": {
        XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
    }}},
)
async def upload_source(
    task_id: UUID, request: Request, operator: Operator,
    expected_revision: Annotated[int, Query(ge=1)], filename: str,
) -> TaskDetails:
    engine = database_for(request)
    storage = storage_for(request)
    async with transaction(engine) as connection:
        current = await owned_revision(connection, task_id, operator)
        if current["revision"] != expected_revision:
            raise HTTPException(409, "Task revision has changed")

    validate_filename(filename)
    data = await read_upload(request)
    await to_thread.run_sync(validate_workbook, data)
    key = f"sources/{task_id}/{uuid4()}.xlsx"
    storage_attempted = False
    try:
        async with transaction(engine) as connection:
            current = await lock_current_revision(connection, task_id, operator, expected_revision)
            storage_attempted = True
            await to_thread.run_sync(storage.put, key, data)
            return await append_revision(connection, current, {
                "status": "source_uploaded",
                "source_key": key,
                "source_filename": filename,
                "source_size": len(data),
                "source_sha256": sha256(data).hexdigest(),
            })
    except SourceAlreadyExists:
        raise HTTPException(503, "Source storage unavailable") from None
    except BaseException as error:
        if storage_attempted:
            try:
                with CancelScope(shield=True):
                    await discard_unreferenced(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("Source cleanup pending for key %s", key)
        if isinstance(error, StorageError):
            raise HTTPException(503, "Source storage unavailable") from None
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
        data = await to_thread.run_sync(storage_for(request).read, row["source_key"], row["source_size"])
    except StorageError:
        raise HTTPException(503, "Source storage unavailable") from None
    if len(data) != row["source_size"] or sha256(data).hexdigest() != row["source_sha256"]:
        raise HTTPException(503, "Source integrity check failed")
    return Response(
        data, media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["source_filename"], safe=""),
            "Cache-Control": "private, no-store",
        },
    )
