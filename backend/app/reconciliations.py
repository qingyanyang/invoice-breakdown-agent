import json
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from anyio import BrokenWorkerProcess, to_process
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import settings
from app.execution import ExecutionFailure, acquire, claim, executing, fence, settle
from app.extractions import ExtractionDetails, extraction_record
from app.operators import Operator
from app.reconcile import reconcile
from app.reconciliation_tables import source_reconciliations
from app.request_receipts import ReceiptKey, RequestKey, bind_receipt_context, fingerprint, save_receipt
from app.tasks import database_for, lock_current_revision, owned_revision, reserve_owned_receipt, transaction
from app.workflows import RevisionRequest

router = APIRouter(prefix="/tasks/{task_id}/reconciliation", tags=["reconciliation"])
SourceKey = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class ScopePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["source", "base_only"] = "source"
    include: list[SourceKey] = Field(default_factory=list, max_length=1000)
    exclude: list[SourceKey] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_refs(self):
        if (
            set(self.include) & set(self.exclude)
            or len(set(self.include)) != len(self.include)
            or len(set(self.exclude)) != len(self.exclude)
            or self.mode == "base_only" and self.include
        ):
            raise ValueError("Scope selections must be distinct and consistent with base scope")
        return self


class ReconcileRequest(RevisionRequest):
    scope: ScopePolicy | None = None


class ReconciliationAnswer(RevisionRequest):
    question_id: UUID
    answer: SourceKey


class ReconciliationDetails(BaseModel):
    task_id: UUID
    revision: int
    extractor_version: str
    source_sha256: str
    generation: UUID
    status: Literal["blocked", "ready_for_classification"]
    scope: ScopePolicy
    decisions: dict
    report: dict


def record_filter(extraction: ExtractionDetails):
    table = source_reconciliations.c
    return (
        table.task_id == extraction.task_id, table.revision == extraction.revision,
        table.extractor_version == extraction.extractor_version,
        table.source_sha256 == extraction.source_sha256,
    )


async def reconciliation_record(connection: AsyncConnection, extraction: ExtractionDetails):
    result = await connection.execute(select(source_reconciliations).where(*record_filter(extraction)))
    row = result.mappings().one_or_none()
    return ReconciliationDetails.model_validate(row) if row is not None else None


async def require_extraction(connection: AsyncConnection, revision) -> ExtractionDetails:
    record = await extraction_record(connection, revision)
    if record is None or record.status != "extracted":
        raise HTTPException(409, "Current revision requires a published extraction")
    return record


def identified_questions(extraction: ExtractionDetails, scope: ScopePolicy, report: dict) -> None:
    scope_key = json.dumps(scope.model_dump(), sort_keys=True, separators=(",", ":"))
    for question in report["questions"]:
        identity = json.dumps([
            str(extraction.task_id), extraction.revision, extraction.extractor_version,
            extraction.source_sha256, scope_key, question["key"], question["options"],
        ], ensure_ascii=False)
        question["id"] = str(uuid5(NAMESPACE_URL, identity))


async def record_outcome(
    connection: AsyncConnection, extraction: ExtractionDetails, scope: ScopePolicy, decisions: dict, report: dict,
) -> ReconciliationDetails:
    identified_questions(extraction, scope, report)
    fields = {
        "status": report["status"], "scope": scope.model_dump(),
        "decisions": decisions, "report": report, "generation": uuid4(),
    }
    statement = insert(source_reconciliations).values(
        task_id=extraction.task_id, revision=extraction.revision, extractor_version=extraction.extractor_version,
        source_sha256=extraction.source_sha256, **fields,
    )
    await connection.execute(statement.on_conflict_do_update(
        index_elements=[
            source_reconciliations.c.task_id, source_reconciliations.c.revision,
            source_reconciliations.c.extractor_version,
        ],
        set_={**fields, "recorded_at": func.now()},
        where=source_reconciliations.c.source_sha256 == statement.excluded.source_sha256,
    ))
    result = await reconciliation_record(connection, extraction)
    if result is None:
        raise HTTPException(409, "Source checksum has changed")
    return result


def reconciliation_change(payload, current, operator):
    if isinstance(payload, ReconcileRequest):
        scope = payload.scope or (current.scope if current else ScopePolicy())
        return scope, current.decisions if current else {}

    question = next(
        (item for item in current.report["questions"] if item["id"] == str(payload.question_id)),
        None,
    ) if current else None
    if question is None:
        raise HTTPException(409, "Question is no longer pending on this revision")
    if question.get("response_type") == "source_correction":
        raise HTTPException(422, "Upload corrected source evidence on a new revision")
    if payload.answer not in question["options"]:
        raise HTTPException(422, "Select one of the offered reconciliation answers")
    return current.scope, {
        **current.decisions,
        question["key"]: {
            "id": question["id"], "kind": question["kind"], "answer": payload.answer,
            "evidence": question["evidence"], "operator": operator,
        },
    }


async def apply_reconciliation(task_id, payload, request, operator, idempotency_key):
    engine = database_for(request)
    operation = "reconciliation_answer" if isinstance(payload, ReconciliationAnswer) else "reconciliation"
    key = ReceiptKey.from_request(operator, operation, idempotency_key, [task_id, payload])
    original_context = None

    async def snapshot(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        extraction = await require_extraction(connection, revision)
        return revision, extraction, await reconciliation_record(connection, extraction)

    async def prepare(connection):
        nonlocal original_context
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return ReconciliationDetails.model_validate(receipt), None
        revision, extraction, current = await snapshot(connection)
        source = [extraction.source_sha256, extraction.extractor_version, current.generation if current else None]
        context = fingerprint(source)
        if original_context is not None and context != original_context:
            raise HTTPException(409, "Reconciliation inputs have changed")
        original_context = context
        await bind_receipt_context(connection, key, context)
        scope, decisions = reconciliation_change(payload, current, operator)
        if current and current.scope == scope and current.decisions == decisions:
            return await save_receipt(connection, key, current), None
        material = [*source, scope, decisions]
        attempt = await claim(connection, revision, operator, operation, material, settings.execution_rules_seconds)
        return (extraction, current, scope, decisions), attempt

    inputs, attempt = await acquire(engine, prepare)
    if attempt is None:
        return inputs
    extraction, previous, scope, decisions = inputs
    async with executing(engine, attempt):
        try:
            report = await to_process.run_sync(
                reconcile, extraction.result, scope.model_dump(),
                {name: decision["answer"] for name, decision in decisions.items()}, cancellable=True,
            )
        except BrokenWorkerProcess:
            raise ExecutionFailure(503, "Reconciliation unavailable", "reconciliation_worker_unavailable", retryable=True) from None
        async with transaction(engine) as connection:
            await reserve_owned_receipt(connection, task_id, key)
            _, current_extraction, current = await snapshot(connection)
            if current_extraction != extraction or current != previous:
                raise HTTPException(409, "Reconciliation inputs have changed")
            await fence(connection, attempt)
            result = await record_outcome(connection, extraction, scope, decisions, report)
            await save_receipt(connection, key, result)
            await settle(
                connection, attempt, "blocked" if result.status == "blocked" else "succeeded",
                result={"status": result.status, "generation": result.generation},
            )
            return result


@router.post("")
async def run_reconciliation(
    task_id: UUID, payload: ReconcileRequest, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> ReconciliationDetails:
    return await apply_reconciliation(task_id, payload, request, operator, idempotency_key)


@router.get("")
async def get_reconciliation(task_id: UUID, request: Request, operator: Operator) -> ReconciliationDetails:
    async with transaction(database_for(request)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        extraction = await require_extraction(connection, revision)
        current = await reconciliation_record(connection, extraction)
    if current is None:
        raise HTTPException(404, "No reconciliation recorded for this revision")
    return current


@router.post("/answers")
async def answer_reconciliation(
    task_id: UUID, payload: ReconciliationAnswer, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> ReconciliationDetails:
    return await apply_reconciliation(task_id, payload, request, operator, idempotency_key)
