import logging
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after
from langgraph.types import Command
from psycopg import Error as PostgresError
from pydantic import BaseModel
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.outcomes import FailureKind, UseCaseError
from app.features.tasks.models import RevisionRequest
from app.checkpoints import open_checkpointer
from app.config import settings
from app.database import DatabaseConfigurationError
from app.execution import acquire, claim, executing, fence, settle
from app.workflows.graph import (
    GST_BASES, CheckpointStateError, GSTAnswer, InvoiceState, compile_invoice_graph, target_evidence,
)
from app.request_receipts import ReceiptKey, reserve_receipt, save_receipt
from app.database import database_for
from app.features.tasks.use_cases import lock_current_revision, owned_revision
from app.execution import transaction
from app.workflows.tables import revision_workflows, workflow_answers

logger = logging.getLogger(__name__)
Stage = Literal["not_started", "awaiting_clarification", "answer_accepted", "ready_for_extraction"]


class AnswerRequest(RevisionRequest):
    question_id: UUID
    answer: GSTAnswer


class TargetEvidence(BaseModel):
    kind: Literal["submitted_target"]
    amount: str
    gst_basis: Literal["unknown"]


class Question(BaseModel):
    id: UUID
    reason: Literal["target_gst_basis_unknown"] = "target_gst_basis_unknown"
    evidence: TargetEvidence
    options: tuple[GSTAnswer, GSTAnswer] = ("ex_gst", "inc_gst")
    answer: GSTAnswer | None = None


class WorkflowDetails(BaseModel):
    task_id: UUID
    revision: int
    status: Stage
    question: Question | None = None
    effective_gst_basis: Literal["excluding_gst", "including_gst"] | None = None


def revision_filter(revision):
    return (
        revision_workflows.c.task_id == revision["task_id"],
        revision_workflows.c.revision == revision["revision"],
    )


async def workflow_record(connection: AsyncConnection, revision):
    result = await connection.execute(
        select(revision_workflows, workflow_answers.c.answer)
        .outerjoin(workflow_answers, revision_workflows.c.question_id == workflow_answers.c.question_id)
        .where(*revision_filter(revision))
    )
    return result.mappings().one_or_none()


def workflow_details(revision, record) -> WorkflowDetails:
    common = {"task_id": revision["task_id"], "revision": revision["revision"]}
    if record is None:
        return WorkflowDetails(**common, status="not_started")

    stage = record["stage"]
    answer = record["answer"]
    if stage == "awaiting_clarification" and answer is not None:
        stage = "answer_accepted"
    question = None
    if record["question_id"] is not None:
        question = Question(
            id=record["question_id"], evidence=record["question_evidence"], answer=answer,
        )
    return WorkflowDetails(
        **common, status=stage, question=question,
        effective_gst_basis=(
            GST_BASES[answer] if answer else revision["target_gst_basis"]
        ) if stage == "ready_for_extraction" else None,
    )


def checkpoint_config(revision):
    return {"configurable": {"thread_id": f"invoice:{revision['task_id']}:{revision['revision']}"}}


def checkpoint_inputs(revision) -> InvoiceState:
    target = revision["target_amount"]
    return {
        "source_sha256": revision["source_sha256"],
        "target_amount": str(target) if target is not None else None,
        "submitted_gst_basis": revision["target_gst_basis"],
    }


@asynccontextmanager
async def durable_graph(revision):
    thread = checkpoint_config(revision)["configurable"]["thread_id"]
    lock = f"{settings.postgres_schema}:{thread}"
    try:
        with fail_after(settings.execution_checkpoint_seconds):
            async with open_checkpointer(settings) as checkpointer:
                connection = checkpointer.conn
                # Keep the advisory lock on the checkpointer's autocommit session.
                # A replacement can claim the task, but it cannot advance this thread
                # until the previous session has finished or disconnected.
                await connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
                try:
                    yield compile_invoice_graph(checkpointer)
                finally:
                    with CancelScope(shield=True):
                        with fail_after(settings.execution_db_timeout_seconds):
                            await connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
    except (PostgresError, DatabaseConfigurationError, CheckpointStateError, TimeoutError) as error:
        logger.warning("Workflow checkpoints unavailable: %s", type(error).__name__)
        raise UseCaseError(FailureKind.UNAVAILABLE, "Workflow checkpoint storage unavailable") from None


@asynccontextmanager
async def active_graph(engine, revision, attempt):
    async with durable_graph(revision) as graph:
        async with transaction(engine) as connection:
            await fence(connection, attempt)
        yield graph


def verify_checkpoint(snapshot, inputs: InvoiceState) -> None:
    if any(snapshot.values.get(name) != value for name, value in inputs.items()):
        raise CheckpointStateError("Checkpoint inputs do not match this revision")


def verify_pending(snapshot, inputs: InvoiceState) -> None:
    verify_checkpoint(snapshot, inputs)
    if (
        inputs["submitted_gst_basis"] != "unknown"
        or inputs["target_amount"] is None
        or len(snapshot.interrupts) != 1
        or snapshot.interrupts[0].value != {
            "reason": "target_gst_basis_unknown", "evidence": target_evidence(inputs),
        }
    ):
        raise CheckpointStateError("Expected a GST clarification interrupt")


def verify_ready(snapshot, inputs: InvoiceState, expected_basis: str | None) -> None:
    verify_checkpoint(snapshot, inputs)
    if (
        snapshot.next or snapshot.tasks or snapshot.interrupts
        or snapshot.values.get("stage") != "ready_for_extraction"
        or snapshot.values.get("effective_gst_basis") != expected_basis
    ):
        raise CheckpointStateError("Checkpoint is not ready for extraction")


async def initialize_workflow(engine, revision, attempt) -> dict:
    config = checkpoint_config(revision)
    inputs = checkpoint_inputs(revision)
    async with active_graph(engine, revision, attempt) as graph:
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts and (snapshot.next or snapshot.tasks):
            # A saved node write may be projected into the snapshot before its next checkpoint.
            await graph.ainvoke(None, config=config)
            snapshot = await graph.aget_state(config)
        elif not snapshot.values and not snapshot.interrupts:
            await graph.ainvoke(inputs, config=config)
            snapshot = await graph.aget_state(config)

        if snapshot.interrupts:
            verify_pending(snapshot, inputs)
            return {
                "stage": "awaiting_clarification", "question_id": uuid4(),
                "question_evidence": target_evidence(inputs),
            }
        verify_ready(snapshot, inputs, inputs["submitted_gst_basis"])
        return {"stage": "ready_for_extraction", "question_id": None}


async def resume_accepted(engine, revision, attempt, answer: GSTAnswer) -> None:
    config = checkpoint_config(revision)
    inputs = checkpoint_inputs(revision)
    async with active_graph(engine, revision, attempt) as graph:
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts and (snapshot.next or snapshot.tasks):
            verify_checkpoint(snapshot, inputs)
            await graph.ainvoke(None, config=config)
            snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            verify_pending(snapshot, inputs)
            await graph.ainvoke(Command(resume=answer), config=config)
            snapshot = await graph.aget_state(config)
        # A previously committed checkpoint can satisfy a later application publication.
        verify_ready(snapshot, inputs, GST_BASES[answer])


async def advance_workflow(
    engine: AsyncEngine, task_id: UUID, operator: str, expected_revision: int,
) -> WorkflowDetails:
    async def prepare(connection):
        revision = await lock_current_revision(connection, task_id, operator, expected_revision)
        if revision["status"] != "source_uploaded" or not revision["source_key"]:
            raise UseCaseError(FailureKind.CONFLICT, "Upload a source before starting the workflow")
        record = await workflow_record(connection, revision)
        detail = workflow_details(revision, record)
        if detail.status not in {"not_started", "answer_accepted"}:
            return detail, None
        operation = "checkpoint_start" if record is None else "checkpoint_resume"
        material = [revision["source_sha256"], detail.question]
        attempt = await claim(connection, revision, operator, operation, material, settings.execution_checkpoint_seconds)
        return (revision, record), attempt

    inputs, attempt = await acquire(engine, prepare)
    if attempt is None:
        return inputs

    revision, record = inputs
    async with executing(engine, attempt):
        if record is None:
            fields = await initialize_workflow(engine, revision, attempt)
        else:
            await resume_accepted(engine, revision, attempt, record["answer"])
        async with transaction(engine) as connection:
            current = await lock_current_revision(connection, task_id, operator, expected_revision)
            await fence(connection, attempt)
            latest = await workflow_record(connection, current)
            if latest != record or current["source_sha256"] != revision["source_sha256"]:
                raise UseCaseError(FailureKind.CONFLICT, "The workflow inputs have changed")
            if record is None:
                await connection.execute(insert(revision_workflows).values(
                    task_id=task_id, revision=expected_revision, **fields,
                ))
            else:
                await connection.execute(update(revision_workflows).where(*revision_filter(current))
                                         .values(stage="ready_for_extraction"))
            result = workflow_details(current, await workflow_record(connection, current))
            await settle(
                connection, attempt, "blocked" if result.status == "awaiting_clarification" else "succeeded",
                result={"status": result.status, "question_id": result.question.id if result.question else None},
            )
            return result


async def get_workflow(task_id: UUID, resources, operator: str) -> WorkflowDetails:
    async with transaction(database_for(resources)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        return workflow_details(revision, await workflow_record(connection, revision))


async def start_or_recover(
    task_id: UUID, payload: RevisionRequest, resources, operator: str,
) -> WorkflowDetails:
    return await advance_workflow(database_for(resources), task_id, operator, payload.expected_revision)


async def pending_question(connection, revision, question_id):
    record = await workflow_record(connection, revision)
    if record is None or record["question_id"] != question_id:
        raise UseCaseError(FailureKind.NOT_FOUND, "Question not found on this revision")
    if record["stage"] != "awaiting_clarification" or record["answer"] is not None:
        raise UseCaseError(FailureKind.CONFLICT, "Question is no longer pending")


async def answer_question(
    task_id: UUID, payload: AnswerRequest, resources, operator: str, idempotency_key: str | None = None,
) -> WorkflowDetails:
    engine = database_for(resources)
    key = ReceiptKey.from_request(operator, "workflow_answer", idempotency_key, [task_id, payload])
    async with transaction(engine) as connection:
        if key is not None:
            await owned_revision(connection, task_id, operator)
            receipt = await reserve_receipt(connection, key)
            if receipt is not None:
                return WorkflowDetails.model_validate(receipt)
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        await pending_question(connection, revision, payload.question_id)

    # Verify the checkpoint outside the application transaction, then accept exactly
    # one answer under the task lock. The durable receipt and accepted answer commit together.
    async with durable_graph(revision) as graph:
        snapshot = await graph.aget_state(checkpoint_config(revision))
        async with transaction(engine) as connection:
            receipt = await reserve_receipt(connection, key)
            if receipt is not None:
                return WorkflowDetails.model_validate(receipt)
            current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
            await pending_question(connection, current, payload.question_id)
            verify_pending(snapshot, checkpoint_inputs(current))
            accepted = await connection.scalar(
                postgres_insert(workflow_answers)
                .values(question_id=payload.question_id, answer=payload.answer)
                .on_conflict_do_nothing(index_elements=[workflow_answers.c.question_id])
                .returning(workflow_answers.c.question_id)
            )
            if accepted is None:
                raise UseCaseError(FailureKind.CONFLICT, "Question is no longer pending")
            await save_receipt(connection, key, workflow_details(current, await workflow_record(connection, current)))

    result = await advance_workflow(engine, task_id, operator, payload.expected_revision)
    if key is not None:
        async with transaction(engine) as connection:
            await save_receipt(connection, key, result)
    return result
