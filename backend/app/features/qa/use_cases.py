"""Run the QA worker and publish its evidence and candidate files."""

import logging
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after, to_thread
from sqlalchemy import insert, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.outcomes import FailureKind, UseCaseError
from app.config import settings
from app.delivery_guard import guard_delivery_inputs
from app.execution import (
    ExecutionFailure, acquire, claim, executing, fence, object_may_be_discarded, register_object, settle,
    write_registered_object,
)
from app.features.qa.checks import MACHINE_CHECKS, QA_VERSION, QAResult, default_checks, inspect_workbook, outcome, review_preview
from app.features.qa.engine import EngineUnavailable, QAEngine
from app.features.qa.records import (
    current_artifact, current_status, latest_run, previous_files_available, response_body, retryable,
    stored_file, verify_object,
)
from app.features.qa.tables import workbook_qa_runs
from app.source_storage import SourceAlreadyExists, SourceIntegrityError, StorageError, StorageNotFound
from app.features.tasks.tables import tasks
from app.database import database_for
from app.features.tasks.use_cases import lock_current_revision, storage_for
from app.execution import transaction
from app.features.workbooks.use_cases import output_storage
from app.features.tasks.models import RevisionRequest

logger = logging.getLogger(__name__)


class QARequest(RevisionRequest):
    expected_artifact_id: UUID


def accounting_evidence(inputs):
    approved = inputs.approved
    return {
        "extraction": inputs.extraction, "reconciliation": inputs.reconciliation, "allocation": inputs.allocation,
        "classification": approved.classification.report, "target": approved.target, "gst_basis": approved.gst_basis,
        "scope": approved.reconciliation.scope.model_dump(),
        "scope_answers": {key: value["answer"] for key, value in approved.reconciliation.decisions.items()},
        "policy": inputs.policy,
    }


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


async def compute_qa(resources, inputs, artifact, previous, attempt) -> QAResult:
    checks = default_checks()
    try:
        output = output_storage(resources)
        with fail_after(25):
            source = await resources.extraction_source_reader.read(
                storage_for(resources), inputs.revision["source_key"],
                inputs.revision["source_size"], inputs.revision["source_sha256"],
            )
            generated = await verify_object(resources, output, artifact)
    except (SourceIntegrityError, StorageNotFound):
        checks["artifact_integrity"] = outcome("failed", "The uploaded source or review workbook is missing or differs from its checksum.")
        return QAResult(checks)
    except (StorageError, UseCaseError, TimeoutError):
        checks["artifact_integrity"] = outcome("not_performed", "Private workbook storage is unavailable.")
        return QAResult(checks)

    engine = BoundedQAWorker(resources.qa_engine, attempt.monotonic_deadline)
    async with resources.qa_limiter:
        if previous and previous["candidate"] and all(
            previous["checks"][name]["status"] == "passed" for name in MACHINE_CHECKS[:-1]
        ):
            try:
                candidate = await verify_object(resources, output, previous["candidate"])
            except (StorageNotFound, SourceIntegrityError):
                candidate = None
            except StorageError:
                checks["artifact_integrity"] = outcome("not_performed", "Private workbook storage is unavailable.")
                return QAResult(checks)
            if candidate is not None:
                resumed = await resources.qa_worker.run(review_preview, candidate, dict(previous["checks"]), engine)
                if not resumed.engine_changed:
                    return resumed
        mapping = {item["original"]: item["copied"] for item in artifact["report"]["source_sheets"]}
        return await resources.qa_worker.run(inspect_workbook, source, generated, accounting_evidence(inputs), mapping, engine)


async def discard_unreferenced_qa(engine, storage, task_id, key):
    async with engine.begin() as connection:
        if not await connection.scalar(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()):
            raise StorageError("The QA task could not be verified.")
        referenced = await connection.scalar(select(workbook_qa_runs.c.id).where(or_(
            workbook_qa_runs.c.candidate["storage_key"].astext == key,
            workbook_qa_runs.c.preview["storage_key"].astext == key,
        )).limit(1))
        if referenced is not None or not await object_may_be_discarded(connection, key):
            return False
    await to_thread.run_sync(storage.delete, key)
    return True


async def last_reviewable_run_id(connection, artifact):
    return await connection.scalar(select(workbook_qa_runs.c.id).filter_by(
        artifact_id=artifact["artifact_id"], artifact_sha256=artifact["sha256"], qa_version=QA_VERSION,
    ).where(workbook_qa_runs.c.status.in_(("awaiting_visual_review", "ready_for_delivery")))
      .order_by(workbook_qa_runs.c.created_at.desc(), workbook_qa_runs.c.id.desc()).limit(1))


async def start_qa(task_id: UUID, payload: QARequest, resources, operator: str):
    engine = database_for(resources)

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
                available = await previous_files_available(resources, output_storage(resources), previous)
            except StorageError:
                raise UseCaseError(FailureKind.UNAVAILABLE, "QA candidate storage is unavailable") from None
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
                raise UseCaseError(FailureKind.CONFLICT, "The current review workbook has changed")
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
        raise UseCaseError(FailureKind.CONFLICT, "The current QA evidence changed while checking storage")

    storage = resources.output_storage
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
            await write_registered_object(engine, attempt, resources.storage_worker, storage, key, data)
        except SourceAlreadyExists:
            written.remove(key)
            raise
        await verify_object(resources, storage, descriptor)
        return descriptor

    try:
        async with executing(engine, attempt):
            computed = await compute_qa(resources, inputs, artifact, previous, attempt)
            async with transaction(engine) as connection:
                _, latest, present = await snapshot(connection)
                if latest["sha256"] != artifact["sha256"] or (present["id"] if present else None) != previous_id:
                    raise UseCaseError(FailureKind.CONFLICT, "The current QA evidence has changed")
                await fence(connection, attempt)

            if storage is None and (computed.candidate is not None or computed.preview is not None):
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "QA candidate storage is unavailable", "qa_storage_unavailable", retryable=True)
            try:
                candidate = None
                if computed.candidate is not None:
                    old_candidate = previous["candidate"] if previous else None
                    if old_candidate and sha256(computed.candidate).hexdigest() == old_candidate["sha256"]:
                        try:
                            await verify_object(resources, storage, old_candidate)
                        except (StorageNotFound, SourceIntegrityError):
                            pass
                        else:
                            candidate = old_candidate
                    if candidate is None:
                        candidate = await stage_file("candidate.xlsx", computed.candidate)
                preview = await stage_file("preview.pdf", computed.preview)
            except StorageError:
                raise ExecutionFailure(FailureKind.UNAVAILABLE, "QA candidate storage is unavailable", "qa_storage_unavailable", retryable=True) from None

            async with transaction(engine) as connection:
                current, latest, present = await snapshot(connection)
                if latest["sha256"] != artifact["sha256"] or (present["id"] if present else None) != previous_id:
                    raise UseCaseError(FailureKind.CONFLICT, "The current QA evidence has changed")
                await fence(connection, attempt)
                await guard_delivery_inputs(connection, task_id)
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
                        if key in resources.storage_worker.writes:
                            logger.warning("QA output cleanup pending for key %s", key)
                        else:
                            await discard_unreferenced_qa(engine, storage, task_id, key)
            except (StorageError, SQLAlchemyError):
                logger.warning("QA output cleanup pending for task %s and run %s", task_id, run_id)
        raise
