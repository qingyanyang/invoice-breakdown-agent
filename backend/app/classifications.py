"""Persist proposals and owner decisions behind the current reconciliation gate."""

import logging
from typing import Literal
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app import classification_model
from app.classification_evidence import ClassificationEvidenceError
from app.classification_model import ClassificationModelError
from app.classification_tables import source_classifications
from app.classify import CLASSIFIER_VERSION, Proposal, build_classification, failure_report, make_evidence
from app.config import settings
from app.extractions import ExtractionDetails
from app.operators import Operator
from app.reconciliations import ReconciliationDetails, reconciliation_record, require_extraction
from app.tasks import database_for, lock_current_revision, owned_revision, transaction
from app.workflows import RevisionRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks/{task_id}/classification", tags=["classification"])


class ClassificationAnswer(RevisionRequest):
    question_id: UUID
    answer: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, min_length=1, max_length=800)


class ClassificationDetails(BaseModel):
    task_id: UUID
    revision: int
    extractor_version: str
    reconciliation_generation: UUID
    source_sha256: str
    status: Literal["failed", "blocked", "ready_for_allocation"]
    classifier_version: str
    skill_sha256: str | None
    model: str | None
    decisions: dict
    report: dict


async def classification_inputs(
    connection: AsyncConnection, revision,
) -> tuple[ExtractionDetails, ReconciliationDetails]:
    extraction = await require_extraction(connection, revision)
    reconciliation = await reconciliation_record(connection, extraction)
    if reconciliation is None or reconciliation.status != "ready_for_classification":
        raise HTTPException(409, "Current revision requires a ready reconciliation")
    return extraction, reconciliation


def identity(reconciliation: ReconciliationDetails):
    return {
        "task_id": reconciliation.task_id,
        "revision": reconciliation.revision,
        "extractor_version": reconciliation.extractor_version,
        "reconciliation_generation": reconciliation.generation,
    }


async def classification_record(connection: AsyncConnection, reconciliation: ReconciliationDetails):
    return (await connection.execute(select(source_classifications).filter_by(
        **identity(reconciliation), source_sha256=reconciliation.source_sha256,
    ))).mappings().one_or_none()


async def save_classification(connection, reconciliation, proposal_id, skill, model, proposal, decisions, report):
    outcome = {
        "classifier_version": CLASSIFIER_VERSION, "status": report["status"],
        "skill_sha256": skill, "model": model, "proposal_id": proposal_id,
        "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
        "report": report, "decisions": decisions,
    }
    statement = insert(source_classifications).values(
        **identity(reconciliation), source_sha256=reconciliation.source_sha256, **outcome,
    )
    row = (await connection.execute(statement.on_conflict_do_update(
        index_elements=tuple(source_classifications.primary_key.columns), set_=outcome,
        where=source_classifications.c.source_sha256 == statement.excluded.source_sha256,
    ).returning(source_classifications))).mappings().one_or_none()
    if row is None:
        raise HTTPException(409, "Source checksum has changed")
    return ClassificationDetails.model_validate(row)


@router.post("")
async def run_classification(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> ClassificationDetails:
    engine = database_for(request)
    async with transaction(engine) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        extraction, reconciliation = await classification_inputs(connection, revision)
        current = await classification_record(connection, reconciliation)
        if current is not None and current["status"] != "failed":
            return ClassificationDetails.model_validate(current)

    skill_sha256 = proposal = None
    model = settings.classification_model
    proposal_id = uuid4()
    try:
        specification = await classification_model.load_primary_specification(settings.classification_skill_path)
        skill_sha256 = specification.sha256
        evidence = await to_thread.run_sync(make_evidence, extraction.result, reconciliation.report)
        inputs = await to_thread.run_sync(evidence.model_input)
        proposal = await classification_model.propose(settings, specification, inputs)
        report = await to_thread.run_sync(build_classification, evidence, proposal, {}, str(proposal_id))
    except (ClassificationEvidenceError, ClassificationModelError) as error:
        logger.warning("Classification unavailable: %s", error.code)
        report = failure_report(error.code)
        proposal = None

    async with transaction(engine) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        _, latest = await classification_inputs(connection, revision)
        if identity(latest) != identity(reconciliation) or latest.source_sha256 != reconciliation.source_sha256:
            raise HTTPException(409, "Current source or reconciliation has changed")
        current = await classification_record(connection, latest)
        if current is not None and current["status"] != "failed":
            return ClassificationDetails.model_validate(current)
        result = await save_classification(
            connection, latest, proposal_id, skill_sha256, model, proposal, {}, report,
        )
    if result.status == "failed":
        raise HTTPException(503, "Classification unavailable. Read the recorded outcome for details.")
    return result


@router.get("")
async def get_classification(task_id: UUID, request: Request, operator: Operator) -> ClassificationDetails:
    async with transaction(database_for(request)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        revision = await lock_current_revision(connection, task_id, operator, revision["revision"])
        _, reconciliation = await classification_inputs(connection, revision)
        current = await classification_record(connection, reconciliation)
    if current is None:
        raise HTTPException(404, "No classification recorded for the current reconciliation")
    return ClassificationDetails.model_validate(current)


@router.post("/answers")
async def answer_classification(
    task_id: UUID, payload: ClassificationAnswer, request: Request, operator: Operator,
) -> ClassificationDetails:
    async with transaction(database_for(request)) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        extraction, reconciliation = await classification_inputs(connection, revision)
        current = await classification_record(connection, reconciliation)
        question = next(
            (item for item in current["report"]["questions"] if item["id"] == str(payload.question_id)),
            None,
        ) if current else None
        if question is None:
            raise HTTPException(409, "Question is no longer pending on this reconciliation")
        if not question["options"]:
            raise HTTPException(409, "Update the authorized scope in reconciliation")
        reason = payload.reason.strip() if payload.reason else None
        if payload.answer not in question["options"] or question["requires_reason"] and not reason:
            raise HTTPException(422, "Select an offered answer and include a reason when required")
        decisions = {
            **current["decisions"],
            question["key"]: {
                "id": question["id"], "kind": question["kind"], "answer": payload.answer,
                "reason": reason, "evidence": question["evidence"], "operator": operator,
            },
        }
        # Replay the persisted proposal with the accepted answer in the same transaction.
        evidence = await to_thread.run_sync(make_evidence, extraction.result, reconciliation.report)
        proposal = Proposal.model_validate(current["proposal"])
        report = await to_thread.run_sync(
            build_classification, evidence, proposal, decisions, str(current["proposal_id"]),
        )
        return await save_classification(
            connection, reconciliation, current["proposal_id"], current["skill_sha256"],
            current["model"], proposal, decisions, report,
        )
