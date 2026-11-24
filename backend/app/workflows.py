import logging
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from langgraph.types import Command
from psycopg import Error as PostgresError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.checkpoints import open_checkpointer
from app.config import settings
from app.database import DatabaseConfigurationError
from app.invoice_graph import (
    GST_BASES, CheckpointStateError, GSTAnswer, InvoiceState, compile_invoice_graph, target_evidence,
)
from app.operators import Operator
from app.tasks import database_for, lock_current_revision, owned_revision, transaction
from app.workflow_tables import revision_workflows, workflow_answers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks/{task_id}/workflow", tags=["workflow"])
Stage = Literal["not_started", "awaiting_clarification", "answer_accepted", "ready_for_extraction"]


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


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
async def durable_graph():
    try:
        async with open_checkpointer(settings) as checkpointer:
            yield compile_invoice_graph(checkpointer)
    except (PostgresError, DatabaseConfigurationError, CheckpointStateError, TimeoutError) as error:
        logger.warning("Workflow checkpoints unavailable: %s", type(error).__name__)
        raise HTTPException(503, "Workflow checkpoint storage unavailable") from None


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


async def initialize_workflow(connection: AsyncConnection, revision) -> WorkflowDetails:
    config = checkpoint_config(revision)
    inputs = checkpoint_inputs(revision)
    async with durable_graph() as graph:
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts and (snapshot.next or snapshot.tasks):
            # A saved node write may already be projected into the snapshot with no next node.
            await graph.ainvoke(None, config=config)
            snapshot = await graph.aget_state(config)
        elif not snapshot.values and not snapshot.interrupts:
            await graph.ainvoke(inputs, config=config)
            snapshot = await graph.aget_state(config)

        if snapshot.interrupts:
            verify_pending(snapshot, inputs)
            fields = {
                "stage": "awaiting_clarification",
                "question_id": uuid4(),
                "question_evidence": target_evidence(inputs),
            }
        else:
            verify_ready(snapshot, inputs, inputs["submitted_gst_basis"])
            fields = {"stage": "ready_for_extraction", "question_id": None}
    await connection.execute(
        insert(revision_workflows).values(task_id=revision["task_id"], revision=revision["revision"], **fields)
    )
    return workflow_details(revision, await workflow_record(connection, revision))


async def resume_accepted(connection: AsyncConnection, revision, answer: GSTAnswer) -> WorkflowDetails:
    config = checkpoint_config(revision)
    inputs = checkpoint_inputs(revision)
    expected_basis = GST_BASES[answer]
    async with durable_graph() as graph:
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts and (snapshot.next or snapshot.tasks):
            verify_checkpoint(snapshot, inputs)
            await graph.ainvoke(None, config=config)
            snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            verify_pending(snapshot, inputs)
            await graph.ainvoke(Command(resume=answer), config=config)
            snapshot = await graph.aget_state(config)
        # If graph progress committed before the task status did, only reconcile the status.
        verify_ready(snapshot, inputs, expected_basis)
    await connection.execute(
        update(revision_workflows).where(*revision_filter(revision)).values(stage="ready_for_extraction")
    )
    return workflow_details(revision, await workflow_record(connection, revision))


async def advance_workflow(
    engine: AsyncEngine, task_id: UUID, operator: str, expected_revision: int,
) -> WorkflowDetails:
    # The same task lock protects revision edits, first start, and all checkpoint advancement.
    async with transaction(engine) as connection:
        revision = await lock_current_revision(connection, task_id, operator, expected_revision)
        if revision["status"] != "source_uploaded" or not revision["source_key"]:
            raise HTTPException(409, "Upload a source before starting the workflow")

        record = await workflow_record(connection, revision)
        if record is None:
            return await initialize_workflow(connection, revision)
        if record["stage"] == "awaiting_clarification" and record["answer"] is not None:
            return await resume_accepted(connection, revision, record["answer"])

        details = workflow_details(revision, record)
        async with durable_graph() as graph:
            snapshot = await graph.aget_state(checkpoint_config(revision))
            inputs = checkpoint_inputs(revision)
            if details.status == "awaiting_clarification":
                verify_pending(snapshot, inputs)
            else:
                verify_ready(snapshot, inputs, details.effective_gst_basis)
        return details


@router.get("")
async def get_workflow(task_id: UUID, request: Request, operator: Operator) -> WorkflowDetails:
    async with transaction(database_for(request)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        return workflow_details(revision, await workflow_record(connection, revision))


@router.post("")
async def start_or_recover(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> WorkflowDetails:
    return await advance_workflow(database_for(request), task_id, operator, payload.expected_revision)


@router.post("/answers")
async def answer_question(
    task_id: UUID, payload: AnswerRequest, request: Request, operator: Operator,
) -> WorkflowDetails:
    engine = database_for(request)
    # Commit acceptance before advancing the checkpointer. The answer row is the recovery trigger.
    async with transaction(engine) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        record = await workflow_record(connection, revision)
        if record is None or record["question_id"] != payload.question_id:
            raise HTTPException(404, "Question not found on this revision")
        if record["stage"] != "awaiting_clarification" or record["answer"] is not None:
            raise HTTPException(409, "Question is no longer pending")
        async with durable_graph() as graph:
            snapshot = await graph.aget_state(checkpoint_config(revision))
            verify_pending(snapshot, checkpoint_inputs(revision))
        accepted = await connection.scalar(
            postgres_insert(workflow_answers)
            .values(question_id=payload.question_id, answer=payload.answer)
            .on_conflict_do_nothing(index_elements=[workflow_answers.c.question_id])
            .returning(workflow_answers.c.question_id)
        )
        if accepted is None:
            raise HTTPException(409, "Question is no longer pending")
    return await advance_workflow(engine, task_id, operator, payload.expected_revision)
