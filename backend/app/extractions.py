import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from uuid import UUID

from anyio import BrokenWorkerProcess, fail_after, to_process, to_thread
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.extraction_reader import EXTRACTOR_VERSION, ExtractionProblem, extract_workbook
from app.extraction_tables import source_extractions
from app.operators import Operator
from app.source_storage import SourceIntegrityError, SourceStorage, StorageError, read_verified_source
from app.tasks import database_for, lock_current_revision, owned_revision, transaction
from app.workflows import RevisionRequest, workflow_record

router = APIRouter(prefix="/tasks/{task_id}/extraction", tags=["extraction"])
EXTRACTION_TIMEOUT_SECONDS = 20
FAILURE_HTTP_STATUS = {
    "invalid_workbook": 422,
    "deadline_exceeded": 504,
    "source_integrity": 503,
    "source_unavailable": 503,
    "worker_unavailable": 503,
}


class SourceReadWorker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extraction-source")
        self.slot = asyncio.Semaphore(1)

    async def read(self, storage: SourceStorage, key: str, size: int, checksum: str) -> bytes:
        await self.slot.acquire()
        try:
            job = asyncio.get_running_loop().run_in_executor(
                self.executor, read_verified_source, storage, key, size, checksum,
            )
        except BaseException:
            self.slot.release()
            raise

        def finished(future):
            self.slot.release()
            if not future.cancelled():
                future.exception()

        job.add_done_callback(finished)
        # The request may time out, but the slot follows the underlying read.
        return await asyncio.shield(job)

    async def close(self) -> None:
        await to_thread.run_sync(self.executor.shutdown)


class ExtractionDetails(BaseModel):
    task_id: UUID
    revision: int
    extractor_version: str
    source_sha256: str
    status: Literal["extracted", "failed"]
    result: dict | None
    error_code: str | None
    error_detail: str | None


async def extraction_record(connection: AsyncConnection, revision) -> ExtractionDetails | None:
    result = await connection.execute(
        select(source_extractions).where(
            source_extractions.c.task_id == revision["task_id"],
            source_extractions.c.revision == revision["revision"],
            source_extractions.c.extractor_version == EXTRACTOR_VERSION,
            source_extractions.c.source_sha256 == revision["source_sha256"],
        )
    )
    row = result.mappings().one_or_none()
    return ExtractionDetails.model_validate(row) if row is not None else None


async def require_extraction_ready(connection: AsyncConnection, revision) -> None:
    workflow = await workflow_record(connection, revision)
    if (
        not revision["source_key"] or not revision["source_sha256"]
        or workflow is None or workflow["stage"] != "ready_for_extraction"
    ):
        raise HTTPException(409, "Workflow must reach ready_for_extraction")


async def extract_source(storage: SourceStorage | None, revision, limiter, reader: SourceReadWorker) -> dict:
    if storage is None:
        raise ExtractionProblem("source_unavailable", "Source storage unavailable")
    try:
        with fail_after(EXTRACTION_TIMEOUT_SECONDS):
            async with limiter:
                data = await reader.read(
                    storage, revision["source_key"], revision["source_size"], revision["source_sha256"],
                )
                return await to_process.run_sync(extract_workbook, data, cancellable=True)
    except SourceIntegrityError:
        raise ExtractionProblem("source_integrity", "Source integrity check failed") from None
    except StorageError:
        raise ExtractionProblem("source_unavailable", "Source storage unavailable") from None
    except TimeoutError:
        raise ExtractionProblem("deadline_exceeded", "Workbook extraction exceeded its execution deadline") from None
    except BrokenWorkerProcess:
        raise ExtractionProblem("worker_unavailable", "Workbook extraction worker unavailable") from None


async def publish_outcome(connection: AsyncConnection, revision, fields: dict) -> ExtractionDetails:
    statement = insert(source_extractions).values(
        task_id=revision["task_id"], revision=revision["revision"],
        source_sha256=revision["source_sha256"], extractor_version=EXTRACTOR_VERSION, **fields,
    )
    await connection.execute(statement.on_conflict_do_update(
        index_elements=[
            source_extractions.c.task_id, source_extractions.c.revision,
            source_extractions.c.extractor_version,
        ],
        set_={
            "status": statement.excluded.status, "result": statement.excluded.result,
            "error_code": statement.excluded.error_code, "error_detail": statement.excluded.error_detail,
            "recorded_at": func.now(),
        },
        where=(
            (source_extractions.c.status == "failed")
            & (source_extractions.c.source_sha256 == statement.excluded.source_sha256)
        ),
    ))
    record = await extraction_record(connection, revision)
    if record is None:
        raise HTTPException(409, "Source checksum has changed")
    return record


@router.post("")
async def run_extraction(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> ExtractionDetails:
    engine = database_for(request)
    async with transaction(engine) as connection:
        original = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        await require_extraction_ready(connection, original)
        existing = await extraction_record(connection, original)
        if existing is not None and existing.status == "extracted":
            return existing

    try:
        result = await extract_source(
            request.app.state.source_storage, original,
            request.app.state.extraction_limiter, request.app.state.extraction_source_reader,
        )
    except ExtractionProblem as error:
        fields = {"status": "failed", "result": None, "error_code": error.code, "error_detail": error.detail}
    else:
        fields = {"status": "extracted", "result": result, "error_code": None, "error_detail": None}

    # Extraction runs outside the task lock. Publish only against the still-current source.
    async with transaction(engine) as connection:
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        if (
            current["source_sha256"] != original["source_sha256"]
            or current["source_key"] != original["source_key"]
        ):
            raise HTTPException(409, "Source checksum has changed")
        await require_extraction_ready(connection, current)
        record = await publish_outcome(connection, current, fields)
    if record.status == "failed":
        raise HTTPException(
            FAILURE_HTTP_STATUS.get(record.error_code, 413), record.error_detail,
        )
    return record


@router.get("")
async def get_extraction(task_id: UUID, request: Request, operator: Operator) -> ExtractionDetails:
    async with transaction(database_for(request)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        record = await extraction_record(connection, revision)
    if record is None:
        raise HTTPException(404, "No extraction recorded for this revision")
    return record
