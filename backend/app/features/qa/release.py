"""Accept visual review and release the exact verified candidate."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import update

from app.delivery_guard import guard_delivery_inputs
from app.execution import transaction
from app.features.qa.checks import outcome
from app.features.qa.records import (
    approved_for_delivery, current_artifact, current_status, latest_run, previous_files_available,
    response_body, verify_object,
)
from app.features.qa.tables import workbook_qa_runs
from app.features.qa.use_cases import QARequest
from app.features.tasks.upload import XLSX_MEDIA_TYPE
from app.database import database_for
from app.features.tasks.use_cases import lock_current_revision, owned_revision
from app.features.workbooks.use_cases import output_storage
from app.outcomes import FailureKind, FileContent, UseCaseError
from app.request_receipts import ReceiptKey, reserve_receipt, save_receipt
from app.source_storage import StorageError


SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SheetReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str = Field(min_length=1, max_length=31)
    outcome: Literal["passed", "failed"]
    note: str | None = Field(default=None, max_length=800)


class VisualReview(QARequest):
    expected_qa_id: UUID
    candidate_sha256: SHA256
    preview_sha256: SHA256
    sheets: list[SheetReview] = Field(min_length=1, max_length=103)


async def get_current_run(engine, task_id, operator):
    async with transaction(engine) as connection:
        revision = await owned_revision(connection, task_id, operator)
        revision = await lock_current_revision(connection, task_id, operator, revision["revision"])
        inputs, artifact = await current_artifact(connection, revision)
        run = await latest_run(connection, artifact)
        if run is None:
            raise UseCaseError(FailureKind.NOT_FOUND, "No QA is recorded for the current review workbook")
    return inputs, artifact, run


async def verify_dispatch(task_id, identity, status, resources, operator):
    """Recheck the exact revision and QA evidence under the dispatch guard."""
    _, artifact, run = await get_current_run(database_for(resources), task_id, operator)
    if (
        artifact["revision"] != identity["revision"] or str(artifact["artifact_id"]) != identity["expected_artifact_id"]
        or str(run["id"]) != identity["expected_qa_id"] or current_status(run["checks"]) != status
        or not run["candidate"] or run["candidate"]["sha256"] != identity["candidate_sha256"]
        or not run["preview"] or run["preview"]["sha256"] != identity["preview_sha256"]
        or (status == "ready_for_delivery" and not approved_for_delivery(run))
    ):
        raise UseCaseError(FailureKind.CONFLICT, "The current artifact or QA run has changed")


async def read_qa(task_id: UUID, resources, operator: str):
    inputs, artifact, run = await get_current_run(database_for(resources), task_id, operator)
    if approved_for_delivery(run):
        try:
            if not await previous_files_available(resources, output_storage(resources), run):
                raise UseCaseError(FailureKind.CONFLICT, "The candidate or preview is unavailable; rerun QA")
        except StorageError:
            raise UseCaseError(FailureKind.UNAVAILABLE, "QA candidate storage is unavailable") from None
    return response_body(run, inputs, artifact)


async def accept_visual_review(
    task_id: UUID, payload: VisualReview, resources, operator: str, idempotency_key: str | None = None,
):
    engine = database_for(resources)
    material = payload.model_dump(mode="python")
    material["sheets"] = sorted(material["sheets"], key=lambda sheet: sheet["sheet"])
    key = ReceiptKey.from_request(operator, "visual_review", idempotency_key, [task_id, material])

    async def review_inputs(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        inputs, artifact = await current_artifact(connection, revision, payload.expected_artifact_id)
        run = await latest_run(connection, artifact)
        if run is None or run["id"] != payload.expected_qa_id:
            raise UseCaseError(FailureKind.CONFLICT, "This QA run is no longer waiting for visual review")
        return inputs, artifact, run

    async with transaction(engine) as connection:
        # Authorization and current artifact checks precede replay of a delivery approval.
        inputs, artifact, run = await review_inputs(connection)
        if (receipt := await reserve_receipt(connection, key)) is not None:
            return receipt
        require_visual_review(run, payload)
        expected = {item["sheet"] for item in run["checks"]["preview_coverage"]["evidence"]["worksheets"]}
        if len(payload.sheets) != len(expected) or {item.sheet for item in payload.sheets} != expected:
            raise UseCaseError(FailureKind.INVALID_DATA, "Confirm visual checks for every recorded worksheet")
        failed = [item for item in payload.sheets if item.outcome == "failed"]
        if any(not (item.note or "").strip() for item in failed):
            raise UseCaseError(FailureKind.INVALID_DATA, "Record the issue for each failed worksheet")

    try:
        available = await previous_files_available(resources, output_storage(resources), run)
    except StorageError:
        raise UseCaseError(FailureKind.UNAVAILABLE, "QA candidate storage is unavailable") from None
    if not available:
        raise UseCaseError(FailureKind.CONFLICT, "The candidate or preview is unavailable; rerun QA")

    async with transaction(engine) as connection:
        inputs, artifact, run = await review_inputs(connection)
        if (receipt := await reserve_receipt(connection, key)) is not None:
            return receipt
        require_visual_review(run, payload)
        await guard_delivery_inputs(connection, task_id)
        approval = {
            "operator": operator, "reviewed_at": datetime.now(UTC).isoformat(),
            "candidate_sha256": payload.candidate_sha256, "preview_sha256": payload.preview_sha256,
            "sheets": [item.model_dump() for item in payload.sheets],
        }
        checks = dict(run["checks"])
        checks["owner_visual_review"] = outcome(
            "failed" if failed else "passed",
            "The owner reported visual defects." if failed else "The owner inspected every recorded worksheet preview.",
            locations=[{"sheet": item.sheet, "detail": item.note} for item in failed],
            evidence={"candidate_sha256": payload.candidate_sha256, "preview_sha256": payload.preview_sha256},
        )
        saved = await connection.execute(update(workbook_qa_runs).where(workbook_qa_runs.c.id == run["id"])
                                         .values(checks=checks, owner_review=approval, status=current_status(checks))
                                         .returning(workbook_qa_runs))
        result = response_body(saved.mappings().one(), inputs, artifact)
        return await save_receipt(connection, key, result)


def require_visual_review(run, payload):
    if current_status(run["checks"]) != "awaiting_visual_review":
        raise UseCaseError(FailureKind.CONFLICT, "This QA run is no longer waiting for visual review")
    if (
        run["candidate"] is None or run["preview"] is None
        or payload.candidate_sha256 != run["candidate"]["sha256"]
        or payload.preview_sha256 != run["preview"]["sha256"]
    ):
        raise UseCaseError(FailureKind.CONFLICT, "The candidate or preview has changed")


async def download_file(task_id, resources, operator, *, final):
    engine = database_for(resources)
    _, artifact, run = await get_current_run(engine, task_id, operator)
    status = current_status(run["checks"])
    if final and not approved_for_delivery(run):
        raise UseCaseError(FailureKind.CONFLICT, "Every required check and owner visual approval must pass before delivery")
    descriptor = run["candidate" if final else "preview"]
    if descriptor is None:
        raise UseCaseError(FailureKind.NOT_FOUND, "No preview or candidate file is recorded for this QA run")
    try:
        data = await verify_object(resources, output_storage(resources), descriptor)
        if final:
            await verify_object(resources, output_storage(resources), run["preview"])
    except StorageError:
        raise UseCaseError(FailureKind.UNAVAILABLE, "The approved candidate or preview failed its storage integrity check") from None
    _, current_artifact_row, current = await get_current_run(engine, task_id, operator)
    if current["id"] != run["id"] or current_artifact_row["artifact_id"] != artifact["artifact_id"]:
        raise UseCaseError(FailureKind.CONFLICT, "The current artifact or QA run has changed")
    if final and not approved_for_delivery(current):
        raise UseCaseError(FailureKind.CONFLICT, "Delivery approval is unavailable for the current QA run")
    filename = artifact["filename"] if final else "Invoice Breakdown QA Preview.pdf"
    return FileContent(data, filename, XLSX_MEDIA_TYPE if final else "application/pdf", status)


async def download_preview(task_id: UUID, resources, operator: str):
    return await download_file(task_id, resources, operator, final=False)


async def download_final(task_id: UUID, resources, operator: str):
    return await download_file(task_id, resources, operator, final=True)
