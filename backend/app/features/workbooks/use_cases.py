"""Generate, publish and download current owner-review workbook artifacts."""

import logging
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePath
from typing import Literal
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after, to_process, to_thread
from anyio.to_process import BrokenWorkerProcess
from pydantic import BaseModel
from sqlalchemy import func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.outcomes import FailureKind, FileContent, UseCaseError
from app.features.allocation.use_cases import AllocationInputs, allocation_record, current_inputs
from app.config import settings
from app.delivery_guard import guard_delivery_inputs
from app.execution import (
    ExecutionFailure, acquire, claim, executing, fence, object_may_be_discarded, register_object, settle,
    write_registered_object,
)
from app.source_storage import (
    SourceAlreadyExists, SourceIntegrityError, StorageError, StorageNotFound, read_verified_source,
)
from app.features.tasks.tables import tasks
from app.database import database_for
from app.features.tasks.use_cases import lock_current_revision, owned_revision, storage_for
from app.execution import transaction
from app.features.workbooks.generate import GENERATOR_VERSION, REVIEW_STATUS, generate_workbook
from app.features.workbooks.layout import TEMPLATE_VERSION
from app.features.workbooks.source import WorkbookProblem
from app.features.workbooks.tables import workbook_artifacts
from app.features.tasks.upload import XLSX_MEDIA_TYPE
from app.features.tasks.models import RevisionRequest

logger = logging.getLogger(__name__)
GENERATION_TIMEOUT = 30


class WorkbookRequest(RevisionRequest):
    expected_allocation_id: UUID


class WorkbookDetails(BaseModel):
    artifact_id: UUID
    task_id: UUID
    revision: int
    source_sha256: str
    allocation_id: UUID
    generator_version: str
    template_version: str
    version: int
    filename: str
    size: int
    sha256: str
    status: Literal["generated_awaiting_qa"] = REVIEW_STATUS
    report: dict


@dataclass(frozen=True)
class CurrentWorkbookInputs:
    revision: dict
    allocation_id: UUID
    extraction: dict
    reconciliation: dict
    allocation: dict
    approved: AllocationInputs
    policy: dict

    @property
    def identity(self) -> dict:
        return {
            "task_id": self.revision["task_id"], "revision": self.revision["revision"],
            "source_sha256": self.revision["source_sha256"], "allocation_id": self.allocation_id,
            "generator_version": GENERATOR_VERSION, "template_version": TEMPLATE_VERSION,
        }


async def workbook_inputs(connection: AsyncConnection, revision, expected: UUID | None = None) -> CurrentWorkbookInputs:
    inputs = await current_inputs(connection, revision)
    record = await allocation_record(connection, inputs, inputs.fingerprint())
    if record is None or record["status"] != "ready_for_workbook":
        raise UseCaseError(FailureKind.CONFLICT, "Current revision requires a ready allocation")
    if expected is not None and record["allocation_id"] != expected:
        raise UseCaseError(FailureKind.CONFLICT, "The approved allocation has changed")
    return CurrentWorkbookInputs(
        dict(revision), record["allocation_id"], inputs.extraction.result, inputs.reconciliation.report, record["report"],
        inputs, record["policy"],
    )


async def find_workbook(connection, inputs):
    result = await connection.execute(
        select(workbook_artifacts).filter_by(**inputs.identity)
        .order_by(workbook_artifacts.c.version.desc()).limit(1)
    )
    return result.mappings().one_or_none()


def output_storage(resources):
    storage = resources.output_storage
    if storage is None:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Workbook storage unavailable")
    return storage


async def workbook_is_reusable(worker, storage, record) -> bool:
    if record is None:
        return False
    try:
        with fail_after(settings.execution_workbook_seconds):
            await worker.run(read_verified_source, storage, record["storage_key"], record["size"], record["sha256"])
    except (StorageNotFound, SourceIntegrityError):
        return False
    except (StorageError, TimeoutError):
        raise UseCaseError(FailureKind.UNAVAILABLE, "Workbook storage unavailable") from None
    return True


def filename_for(project_name: str | None, source_name: str, revision: int, version: int) -> str:
    label = project_name or PurePath(source_name).stem
    cleaned = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', " ", label)
    cleaned = " ".join(cleaned.split()).strip(". ")[:96].rstrip(". ")
    return f"{cleaned or 'Project'} Invoice Breakdown r{revision} v{version}.xlsx"


async def discard_unreferenced_workbook(engine, storage, task_id: UUID, key: str) -> bool:
    # A failed commit can have succeeded on the server. Reconnect before deleting.
    async with engine.begin() as connection:
        task = await connection.scalar(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update())
        if task is None:
            raise StorageError("Workbook task could not be verified")
        existing = await connection.scalar(
            select(workbook_artifacts.c.artifact_id).where(workbook_artifacts.c.storage_key == key)
        )
        if existing is not None or not await object_may_be_discarded(connection, key):
            return False
    await to_thread.run_sync(storage.delete, key)
    return True


async def create_workbook(task_id: UUID, payload: WorkbookRequest, resources, operator: str) -> WorkbookDetails:
    engine = database_for(resources)
    worker = resources.storage_worker

    async def snapshot(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        return await workbook_inputs(connection, revision, payload.expected_allocation_id)

    for _ in range(3):
        async with transaction(engine) as connection:
            original = await snapshot(connection)
            existing = await find_workbook(connection, original)

        storage = output_storage(resources)
        if await workbook_is_reusable(worker, storage, existing):
            async with transaction(engine) as connection:
                current = await snapshot(connection)
                latest = await find_workbook(connection, current)
                if current.identity == original.identity and latest is not None and latest["artifact_id"] == existing["artifact_id"]:
                    return WorkbookDetails.model_validate(latest)
            continue

        prior_id = existing["artifact_id"] if existing is not None else None

        async def prepare(connection):
            current = await snapshot(connection)
            if current.identity != original.identity:
                raise UseCaseError(FailureKind.CONFLICT, "Workbook generation inputs have changed")
            latest = await find_workbook(connection, current)
            if (latest["artifact_id"] if latest is not None else None) != prior_id:
                return None, None
            attempt = await claim(
                connection, current.revision, operator, "workbook",
                [current.identity, prior_id], settings.execution_workbook_seconds,
            )
            return None, attempt

        _, attempt = await acquire(engine, prepare)
        if attempt is not None:
            break
    else:
        raise UseCaseError(FailureKind.CONFLICT, "The review workbook changed while checking storage")

    revision = original.revision
    artifact_id = uuid4()
    key = f"outputs/{task_id}/{artifact_id}.xlsx"
    attempted = False
    try:
        async with executing(engine, attempt):
            try:
                with fail_after(GENERATION_TIMEOUT):
                    async with resources.workbook_limiter:
                        data = await resources.extraction_source_reader.read(
                            storage_for(resources), revision["source_key"], revision["source_size"], revision["source_sha256"],
                        )
                        generated = await to_process.run_sync(
                            generate_workbook, data, original.extraction, original.reconciliation, original.allocation,
                            cancellable=True,
                        )
            except WorkbookProblem as error:
                raise ExecutionFailure(FailureKind.INVALID_DATA, error.report(), error.code) from None
            except TimeoutError:
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Workbook generation exceeded its time limit", "generation_timeout", retryable=True) from None
            except SourceIntegrityError:
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Source integrity check failed", "source_integrity") from None
            except (StorageError, BrokenWorkerProcess):
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Workbook generation unavailable", "generation_unavailable", retryable=True) from None

            async with transaction(engine) as connection:
                current = await snapshot(connection)
                if current.identity != original.identity:
                    raise UseCaseError(FailureKind.CONFLICT, "Workbook generation inputs have changed")
                await fence(connection, attempt)

            checksum, size = sha256(generated.data).hexdigest(), len(generated.data)
            await register_object(engine, attempt, key)
            attempted = True
            try:
                await write_registered_object(engine, attempt, worker, storage, key, generated.data)
                await worker.run(read_verified_source, storage, key, size, checksum)
            except SourceAlreadyExists:
                attempted = False
                raise ExecutionFailure(FailureKind.CONFLICT, "Workbook storage key already exists; retry generation", "storage_collision", retryable=True) from None
            except StorageError:
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "Workbook storage unavailable", "output_storage_unavailable", retryable=True) from None

            async with transaction(engine) as connection:
                current = await snapshot(connection)
                if current.identity != original.identity:
                    raise UseCaseError(FailureKind.CONFLICT, "Workbook generation inputs have changed")
                await fence(connection, attempt)
                latest = await find_workbook(connection, current)
                if (latest["artifact_id"] if latest is not None else None) != prior_id:
                    raise UseCaseError(FailureKind.CONFLICT, "The review workbook has changed")
                latest_version = await connection.scalar(
                    select(func.max(workbook_artifacts.c.version)).where(workbook_artifacts.c.task_id == task_id)
                )
                version = (latest_version or 0) + 1
                await guard_delivery_inputs(connection, task_id)
                result = await connection.execute(insert(workbook_artifacts).values(
                    **current.identity, artifact_id=artifact_id, version=version, storage_key=key, size=size, sha256=checksum,
                    filename=filename_for(generated.project_name, revision["source_filename"], revision["revision"], version),
                    report={**generated.report, "original_source_integrity": "sha256_matched_before_generation"},
                ).returning(workbook_artifacts))
                await settle(connection, attempt, "succeeded", result={
                    "artifact_id": artifact_id, "version": version, "sha256": checksum,
                })
                return WorkbookDetails.model_validate(result.mappings().one())
    except BaseException:
        if attempted and key in worker.writes:
            logger.warning("Workbook cleanup pending for key %s", key)
        elif attempted:
            try:
                with CancelScope(shield=True):
                    await discard_unreferenced_workbook(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("Workbook cleanup pending for key %s", key)
        raise


async def current_workbook(engine, task_id, operator):
    async with transaction(engine) as connection:
        revision = await owned_revision(connection, task_id, operator)
        revision = await lock_current_revision(connection, task_id, operator, revision["revision"])
        inputs = await workbook_inputs(connection, revision)
        row = await find_workbook(connection, inputs)
    if row is None:
        raise UseCaseError(FailureKind.NOT_FOUND, "No workbook recorded for the current allocation")
    return row


async def get_workbook(task_id: UUID, resources, operator: str) -> WorkbookDetails:
    return WorkbookDetails.model_validate(await current_workbook(database_for(resources), task_id, operator))


async def download_workbook(task_id: UUID, resources, operator: str) -> FileContent:
    engine = database_for(resources)
    row = await current_workbook(engine, task_id, operator)
    try:
        data = await to_thread.run_sync(read_verified_source, output_storage(resources), row["storage_key"], row["size"], row["sha256"])
    except StorageError:
        raise UseCaseError(FailureKind.UNAVAILABLE, "Workbook integrity or storage check failed") from None
    latest = await current_workbook(engine, task_id, operator)
    if latest["artifact_id"] != row["artifact_id"]:
        raise UseCaseError(FailureKind.CONFLICT, "The review workbook has changed")
    return FileContent(data, row["filename"], XLSX_MEDIA_TYPE, REVIEW_STATUS)
