"""Persist exact QA evidence, collect owner approval and serve approved bytes."""

import logging
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after, to_thread
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.execution import (
    ExecutionFailure, acquire, claim, executing, fence, final_object_key, forget_colliding_object, object_has_live_attempt, register_object, settle,
)
from app.operators import Operator
from app.qa_checks import CHECKS, MACHINE_CHECKS, QA_VERSION, QAResult, default_checks, inspect_workbook, outcome, review_preview
from app.qa_engine import EngineUnavailable, QAEngine
from app.qa_tables import workbook_qa_runs
from app.request_receipts import ReceiptKey, RequestKey, reserve_receipt, save_receipt
from app.source_storage import SourceAlreadyExists, SourceIntegrityError, StorageError, StorageNotFound, read_verified_source
from app.task_tables import tasks
from app.tasks import database_for, lock_current_revision, owned_revision, storage_for, transaction
from app.workbook_layout import metadata
from app.workbook_upload import XLSX_MEDIA_TYPE
from app.workbooks import find_workbook, output_storage, workbook_inputs
from app.workflows import RevisionRequest

router = APIRouter(prefix="/tasks/{task_id}/delivery", tags=["workbook QA and delivery"])
logger = logging.getLogger(__name__)
SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class QARequest(RevisionRequest):
    expected_artifact_id: UUID


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


def current_status(checks: dict) -> str:
    if all(checks[name]["status"] == "passed" for name in CHECKS):
        return "ready_for_delivery"
    if all(checks[name]["status"] == "passed" for name in MACHINE_CHECKS) and (
        checks["owner_visual_review"]["status"] == "not_performed"
    ):
        return "awaiting_visual_review"
    return "blocked"


def approved_for_delivery(record) -> bool:
    approval, candidate, preview = record["owner_review"], record["candidate"], record["preview"]
    return (
        current_status(record["checks"]) == "ready_for_delivery"
        and approval is not None and candidate is not None and preview is not None
        and approval["candidate_sha256"] == candidate["sha256"]
        and approval["preview_sha256"] == preview["sha256"]
    )


def retryable(record) -> bool:
    if record is None:
        return True
    statuses = [record["checks"][name]["status"] for name in MACHINE_CHECKS]
    return record["status"] == "blocked" and "failed" not in statuses and "not_performed" in statuses


async def current_artifact(connection, revision, expected=None):
    inputs = await workbook_inputs(connection, revision)
    record = await find_workbook(connection, inputs)
    if record is None:
        raise HTTPException(409, "Current allocation requires a generated review workbook")
    if expected is not None and record["artifact_id"] != expected:
        raise HTTPException(409, "The current review workbook has changed")
    return inputs, dict(record)


async def latest_run(connection, artifact):
    result = await connection.execute(
        select(workbook_qa_runs).filter_by(
            artifact_id=artifact["artifact_id"], artifact_sha256=artifact["sha256"], qa_version=QA_VERSION,
        ).order_by(workbook_qa_runs.c.created_at.desc(), workbook_qa_runs.c.id.desc()).limit(1)
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def stored_file(key, data):
    return {"storage_key": key, "size": len(data), "sha256": sha256(data).hexdigest()}


def expose_file(file):
    return {"size": file["size"], "sha256": file["sha256"]} if file is not None else None


def summary_in_chinese(inputs, download_path):
    recon, report = inputs.reconciliation, inputs.allocation
    fields, _ = metadata(inputs.extraction)
    totals = report["display"]["trade_totals"]
    result = {
        "项目": fields.get("project_name") or "源文件未注明",
        "工作范围源金额": report["working_total"],
        "最终金额": report["display"]["total"],
        "Make Good": totals.get("Make Good", "0.00"),
        "Access Panel": totals.get("Access Panel", "0.00"),
        "最终核对差额": report["display"]["rounding"]["difference"],
        "文件": download_path,
    }
    if report["target"] is not None:
        result["目标金额"] = report["target"]
    if factor := report.get("factor"):
        result["调整系数"] = f"{factor['numerator']} ÷ {factor['denominator']}"
    if recon["scope_additions"] or recon["scope_exclusions"]:
        result["原始源表总额"] = recon["reported_total"]
    if inputs.approved.classification.decisions or report["warnings"]:
        result["审核事项"] = {
            "分类澄清": list(inputs.approved.classification.decisions),
            "分配提示": report["warnings"],
        }
    return result


def response_body(record, inputs, artifact):
    status = current_status(record["checks"])
    body = {
        "qa_id": record["id"], "status": status, "qa_version": record["qa_version"],
        "artifact_id": artifact["artifact_id"], "artifact_sha256": artifact["sha256"],
        "revision": artifact["revision"], "allocation_id": artifact["allocation_id"],
        "source_sha256": artifact["source_sha256"], "checks": record["checks"],
        "candidate": expose_file(record["candidate"]), "preview": expose_file(record["preview"]),
        "owner_review": record["owner_review"],
    }
    if approved_for_delivery(record):
        path = f"/tasks/{artifact['task_id']}/delivery/download"
        body["delivery_summary"] = summary_in_chinese(inputs, path)
    return body


def accounting_evidence(inputs):
    approved = inputs.approved
    return {
        "extraction": inputs.extraction, "reconciliation": inputs.reconciliation, "allocation": inputs.allocation,
        "classification": approved.classification.report, "target": approved.target, "gst_basis": approved.gst_basis,
        "scope": approved.reconciliation.scope.model_dump(),
        "scope_answers": {key: value["answer"] for key, value in approved.reconciliation.decisions.items()},
        "policy": inputs.policy,
    }


async def verify_object(request, storage, descriptor) -> bytes:
    return await request.app.state.storage_worker.run(
        read_verified_source, storage, descriptor["storage_key"], descriptor["size"], descriptor["sha256"],
    )


async def previous_files_available(request, storage, record) -> bool:
    if record is None or record["candidate"] is None:
        return False
    try:
        with fail_after(25):
            await verify_object(request, storage, record["candidate"])
            if record["preview"] is not None:
                await verify_object(request, storage, record["preview"])
    except (SourceIntegrityError, StorageNotFound):
        return False
    except TimeoutError:
        raise StorageError from None
    return True


class BoundedQAWorker:
    def __init__(self, engine, deadline):
        self.engine = engine
        self.deadline = deadline

    def run(self, operation, data):
        if monotonic() >= self.deadline:
            raise EngineUnavailable("Workbook QA exceeded its execution budget.")
        if isinstance(self.engine, QAEngine):
            return self.engine.run(operation, data, deadline=self.deadline)
        return self.engine.run(operation, data)


async def compute_qa(request, inputs, artifact, previous, attempt) -> QAResult:
    checks = default_checks()
    try:
        output = output_storage(request)
        with fail_after(25):
            source = await request.app.state.extraction_source_reader.read(
                storage_for(request), inputs.revision["source_key"],
                inputs.revision["source_size"], inputs.revision["source_sha256"],
            )
            generated = await verify_object(request, output, artifact)
    except (SourceIntegrityError, StorageNotFound):
        checks["artifact_integrity"] = outcome("failed", "The uploaded source or review workbook is missing or differs from its checksum.")
        return QAResult(checks)
    except (StorageError, HTTPException, TimeoutError):
        checks["artifact_integrity"] = outcome("not_performed", "Private workbook storage is unavailable.")
        return QAResult(checks)

    engine = BoundedQAWorker(request.app.state.qa_engine, attempt.monotonic_deadline)
    async with request.app.state.qa_limiter:
        if previous and previous["candidate"] and all(
            previous["checks"][name]["status"] == "passed" for name in MACHINE_CHECKS[:-1]
        ):
            try:
                candidate = await verify_object(request, output, previous["candidate"])
            except (StorageNotFound, SourceIntegrityError):
                candidate = None
            except StorageError:
                checks["artifact_integrity"] = outcome("not_performed", "Private workbook storage is unavailable.")
                return QAResult(checks)
            if candidate is not None:
                resumed = await request.app.state.qa_worker.run(review_preview, candidate, dict(previous["checks"]), engine)
                if not resumed.engine_changed:
                    return resumed
        mapping = {item["original"]: item["copied"] for item in artifact["report"]["source_sheets"]}
        return await request.app.state.qa_worker.run(inspect_workbook, source, generated, accounting_evidence(inputs), mapping, engine)


async def discard_unreferenced_qa(engine, storage, task_id, key):
    async with engine.begin() as connection:
        if not await connection.scalar(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()):
            raise StorageError("The QA task could not be verified.")
        referenced = await connection.scalar(select(workbook_qa_runs.c.id).where(or_(
            workbook_qa_runs.c.candidate["storage_key"].astext == key,
            workbook_qa_runs.c.preview["storage_key"].astext == key,
        )).limit(1))
        if referenced is not None or await object_has_live_attempt(connection, final_object_key(key)):
            return False
    await to_thread.run_sync(storage.delete, key)
    return True


async def last_reviewable_run_id(connection, artifact):
    return await connection.scalar(select(workbook_qa_runs.c.id).filter_by(
        artifact_id=artifact["artifact_id"], artifact_sha256=artifact["sha256"], qa_version=QA_VERSION,
    ).where(workbook_qa_runs.c.status.in_(("awaiting_visual_review", "ready_for_delivery")))
      .order_by(workbook_qa_runs.c.created_at.desc(), workbook_qa_runs.c.id.desc()).limit(1))


@router.post("/qa")
async def start_qa(task_id: UUID, payload: QARequest, request: Request, operator: Operator):
    engine = database_for(request)

    async def snapshot(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        inputs, artifact = await current_artifact(connection, revision, payload.expected_artifact_id)
        return inputs, artifact, await latest_run(connection, artifact)

    for _ in range(3):
        async with transaction(engine) as connection:
            inputs, artifact, previous = await snapshot(connection)

        if previous and not retryable(previous):
            if previous["status"] == "blocked":
                return response_body(previous, inputs, artifact)
            try:
                available = await previous_files_available(request, output_storage(request), previous)
            except StorageError:
                raise HTTPException(503, "QA candidate storage is unavailable") from None
            if available:
                async with transaction(engine) as connection:
                    current, latest, present = await snapshot(connection)
                    if present and present["id"] == previous["id"]:
                        return response_body(present, current, latest)
                continue

        previous_id = previous["id"] if previous else None

        async def prepare(connection):
            current, latest, present = await snapshot(connection)
            if latest["sha256"] != artifact["sha256"]:
                raise HTTPException(409, "The current review workbook has changed")
            if (present["id"] if present else None) != previous_id:
                return None, None
            # Stable identity across partial runs, with a fresh budget after a
            # previously reviewable artifact needs storage repair.
            repair = await last_reviewable_run_id(connection, latest)
            attempt = await claim(
                connection, current.revision, operator, "workbook_qa",
                [latest["artifact_id"], latest["sha256"], QA_VERSION, repair,
                 settings.qa_worker_image, settings.qa_docker_runtime],
                settings.execution_qa_seconds,
            )
            return None, attempt

        _, attempt = await acquire(engine, prepare)
        if attempt is not None:
            break
    else:
        raise HTTPException(409, "The current QA evidence changed while checking storage")

    storage = request.app.state.output_storage
    run_id = uuid4()
    written = []

    async def stage_file(label, data):
        if data is None:
            return None
        key = f"qa/{task_id}/{run_id}/{label}"
        descriptor = stored_file(key, data)
        await register_object(engine, attempt, key)
        written.append(key)
        try:
            await request.app.state.storage_worker.run(storage.put, key, data, write_key=key)
        except SourceAlreadyExists:
            written.remove(key)
            await forget_colliding_object(engine, attempt, key)
            raise
        await verify_object(request, storage, descriptor)
        return descriptor

    try:
        async with executing(engine, attempt):
            computed = await compute_qa(request, inputs, artifact, previous, attempt)
            async with transaction(engine) as connection:
                _, latest, present = await snapshot(connection)
                if latest["sha256"] != artifact["sha256"] or (present["id"] if present else None) != previous_id:
                    raise HTTPException(409, "The current QA evidence has changed")
                await fence(connection, attempt)

            if storage is None and (computed.candidate is not None or computed.preview is not None):
                raise ExecutionFailure(503, "QA candidate storage is unavailable", "qa_storage_unavailable", retryable=True)
            try:
                candidate = None
                if computed.candidate is not None:
                    old_candidate = previous["candidate"] if previous else None
                    if old_candidate and sha256(computed.candidate).hexdigest() == old_candidate["sha256"]:
                        try:
                            await verify_object(request, storage, old_candidate)
                        except (StorageNotFound, SourceIntegrityError):
                            pass
                        else:
                            candidate = old_candidate
                    if candidate is None:
                        candidate = await stage_file("candidate.xlsx", computed.candidate)
                preview = await stage_file("preview.pdf", computed.preview)
            except StorageError:
                raise ExecutionFailure(503, "QA candidate storage is unavailable", "qa_storage_unavailable", retryable=True) from None

            async with transaction(engine) as connection:
                current, latest, present = await snapshot(connection)
                if latest["sha256"] != artifact["sha256"] or (present["id"] if present else None) != previous_id:
                    raise HTTPException(409, "The current QA evidence has changed")
                await fence(connection, attempt)
                values = {
                    "id": run_id, "artifact_id": latest["artifact_id"], "artifact_sha256": latest["sha256"],
                    "qa_version": QA_VERSION, "status": current_status(computed.checks), "checks": computed.checks,
                    "candidate": candidate, "preview": preview,
                }
                result = await connection.execute(insert(workbook_qa_runs).values(values).returning(workbook_qa_runs))
                recorded = {"status": values["status"], "qa_id": run_id, "artifact_id": latest["artifact_id"]}
                if retryable(values):
                    await settle(connection, attempt, "retryable", "qa_dependency_unavailable", result=recorded)
                elif values["status"] == "blocked":
                    await settle(connection, attempt, "blocked", "qa_check_failed", result=recorded)
                else:
                    await settle(connection, attempt, "succeeded", result=recorded)
                return response_body(result.mappings().one(), current, latest)
    except BaseException:
        if written:
            try:
                with CancelScope(shield=True):
                    for key in written:
                        if key in request.app.state.storage_worker.writes:
                            logger.warning("QA output cleanup pending for key %s", key)
                        else:
                            await discard_unreferenced_qa(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("QA output cleanup pending for task %s and run %s", task_id, run_id)
        raise


async def get_current_run(engine, task_id, operator):
    async with transaction(engine) as connection:
        revision = await owned_revision(connection, task_id, operator)
        revision = await lock_current_revision(connection, task_id, operator, revision["revision"])
        inputs, artifact = await current_artifact(connection, revision)
        run = await latest_run(connection, artifact)
        if run is None:
            raise HTTPException(404, "No QA is recorded for the current review workbook")
    return inputs, artifact, run


@router.get("/qa")
async def read_qa(task_id: UUID, request: Request, operator: Operator):
    inputs, artifact, run = await get_current_run(database_for(request), task_id, operator)
    if approved_for_delivery(run):
        try:
            if not await previous_files_available(request, output_storage(request), run):
                raise HTTPException(409, "The candidate or preview is unavailable; rerun QA")
        except StorageError:
            raise HTTPException(503, "QA candidate storage is unavailable") from None
    return response_body(run, inputs, artifact)


@router.post("/qa/visual-review")
async def accept_visual_review(
    task_id: UUID, payload: VisualReview, request: Request, operator: Operator, idempotency_key: RequestKey = None,
):
    engine = database_for(request)
    material = payload.model_dump(mode="python")
    material["sheets"] = sorted(material["sheets"], key=lambda sheet: sheet["sheet"])
    key = ReceiptKey.from_request(operator, "visual_review", idempotency_key, [task_id, material])

    async def review_inputs(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        inputs, artifact = await current_artifact(connection, revision, payload.expected_artifact_id)
        run = await latest_run(connection, artifact)
        if run is None or run["id"] != payload.expected_qa_id:
            raise HTTPException(409, "This QA run is no longer waiting for visual review")
        return inputs, artifact, run

    async with transaction(engine) as connection:
        # Authorization and current artifact checks precede replay of a delivery approval.
        inputs, artifact, run = await review_inputs(connection)
        if (receipt := await reserve_receipt(connection, key)) is not None:
            return receipt
        require_visual_review(run, payload)
        expected = {item["sheet"] for item in run["checks"]["preview_coverage"]["evidence"]["worksheets"]}
        if len(payload.sheets) != len(expected) or {item.sheet for item in payload.sheets} != expected:
            raise HTTPException(422, "Confirm visual checks for every recorded worksheet")
        failed = [item for item in payload.sheets if item.outcome == "failed"]
        if any(not (item.note or "").strip() for item in failed):
            raise HTTPException(422, "Record the issue for each failed worksheet")

    try:
        available = await previous_files_available(request, output_storage(request), run)
    except StorageError:
        raise HTTPException(503, "QA candidate storage is unavailable") from None
    if not available:
        raise HTTPException(409, "The candidate or preview is unavailable; rerun QA")

    async with transaction(engine) as connection:
        inputs, artifact, run = await review_inputs(connection)
        if (receipt := await reserve_receipt(connection, key)) is not None:
            return receipt
        require_visual_review(run, payload)
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
        raise HTTPException(409, "This QA run is no longer waiting for visual review")
    if (
        run["candidate"] is None or run["preview"] is None
        or payload.candidate_sha256 != run["candidate"]["sha256"]
        or payload.preview_sha256 != run["preview"]["sha256"]
    ):
        raise HTTPException(409, "The candidate or preview has changed")


async def download_file(task_id, request, operator, *, final):
    engine = database_for(request)
    _, artifact, run = await get_current_run(engine, task_id, operator)
    status = current_status(run["checks"])
    if final and not approved_for_delivery(run):
        raise HTTPException(409, "Every required check and owner visual approval must pass before delivery")
    descriptor = run["candidate" if final else "preview"]
    if descriptor is None:
        raise HTTPException(404, "No preview or candidate file is recorded for this QA run")
    try:
        data = await verify_object(request, output_storage(request), descriptor)
        if final:
            await verify_object(request, output_storage(request), run["preview"])
    except StorageError:
        raise HTTPException(503, "The approved candidate or preview failed its storage integrity check") from None
    _, current_artifact_row, current = await get_current_run(engine, task_id, operator)
    if current["id"] != run["id"] or current_artifact_row["artifact_id"] != artifact["artifact_id"]:
        raise HTTPException(409, "The current artifact or QA run has changed")
    if final and not approved_for_delivery(current):
        raise HTTPException(409, "Delivery approval is unavailable for the current QA run")
    filename = artifact["filename"] if final else "Invoice Breakdown QA Preview.pdf"
    return Response(data, media_type=XLSX_MEDIA_TYPE if final else "application/pdf", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
        "Cache-Control": "private, no-store",
        "X-Workbook-Status": status,
    })


@router.get("/qa/preview", response_class=Response)
async def download_preview(task_id: UUID, request: Request, operator: Operator):
    return await download_file(task_id, request, operator, final=False)


@router.get("/download", response_class=Response)
async def download_final(task_id: UUID, request: Request, operator: Operator):
    return await download_file(task_id, request, operator, final=True)
