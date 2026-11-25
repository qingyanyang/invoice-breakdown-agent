"""Persist proposals and owner decisions behind the current reconciliation gate."""

import logging
from dataclasses import asdict, replace
from typing import Literal
from uuid import UUID, uuid4

from anyio import BrokenWorkerProcess, to_process, to_thread
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app import classification_model, rule_retrieval
from app.classification_evidence import ClassificationEvidenceError
from app.classification_model import ClassificationModelError
from app.classification_tables import source_classifications
from app.classify import CLASSIFIER_VERSION, Proposal, build_classification, failure_report, make_evidence
from app.config import settings
from app.execution import ExecutionFailure, acquire, claim, executing, fence, settle
from app.extractions import ExtractionDetails
from app.operators import Operator
from app.reconciliations import ReconciliationDetails, reconciliation_record, require_extraction
from app.reconciliation_evidence import Reference
from app.request_receipts import ReceiptKey, RequestKey, save_receipt
from app.task_rules import (
    EVALUATION_DATA_ERRORS, PINNED_SUPPLEMENT_ERRORS, RULE_INPUT_ERRORS, applied_supplementary,
    load_verified_rules, primary_document,
    rule_pin, save_pin,
)
from app.tasks import database_for, lock_current_revision, owned_revision, reserve_owned_receipt, transaction
from app.workflows import RevisionRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks/{task_id}/classification", tags=["classification"])
TRANSIENT_CLASSIFICATION_ERRORS = {
    "model_unavailable", "invalid_model_proposal", "primary_specification_unavailable", "pinned_supplementary_unavailable",
}


class ClassificationAnswer(RevisionRequest):
    question_id: UUID
    answer: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, min_length=1, max_length=800)


class ClassificationDetails(BaseModel):
    task_id: UUID
    revision: int
    extractor_version: str
    reconciliation_generation: UUID
    proposal_id: UUID
    source_sha256: str
    status: Literal["failed", "blocked", "ready_for_allocation"]
    classifier_version: str
    skill_sha256: str | None
    model: str | None
    decisions: dict
    report: dict

    @field_validator("report")
    @classmethod
    def omit_legacy_model_rationales(cls, report):
        proposals = report.get("model_proposals")
        if proposals is None or report.get("rationale_origin") == "current_workbook":
            return report
        # Reports and historical retry receipts can predate source-grounded
        # rationales. Their decisions are retained, but free text is unverified.
        return {**report, "model_proposals": [
            {name: value for name, value in proposal.items() if name != "reason"} for proposal in proposals
        ]}


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
        **identity(reconciliation), source_sha256=reconciliation.source_sha256, classifier_version=CLASSIFIER_VERSION,
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


def search_inputs(model_inputs, reconciliation):
    scope_rows = [
        {"row": Reference.from_dict(item["reference"]).key, "description": item["description"]}
        for name in ("scope_additions", "scope_exclusions", "outside_source_scope")
        for item in reconciliation.report[name][:rule_retrieval.MAX_QUERY_ROWS]
        if not rule_retrieval.HISTORY.search(item["description"])
    ]
    return {**model_inputs, "scope_rows": scope_rows}


@router.post("")
async def run_classification(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> ClassificationDetails:
    engine = database_for(request)

    async with transaction(engine) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        _, original_reconciliation = await classification_inputs(connection, revision)
        existing = await classification_record(connection, original_reconciliation)
        if existing is not None and existing["status"] != "failed":
            return ClassificationDetails.model_validate(existing)
        original_pin = await rule_pin(connection, revision)

    verified = rules_error = None
    try:
        verified = await load_verified_rules(request, original_pin)
    except ClassificationModelError as error:
        rules_error = error

    expected_primary = (
        original_pin.primary if original_pin else primary_document(
            settings.classification_skill_sha256, settings.classification_skill_version,
        )
    )
    primary = verified.pin.primary if verified else expected_primary
    supplement = applied_supplementary(verified.specification) if verified else None
    pin = verified.pin if verified else original_pin
    rule_identity = {
        "primary": asdict(primary), "supplementary": asdict(supplement) if supplement else None,
        "pinned_supplementary": asdict(pin.supplementary) if pin and pin.supplementary else None,
        "retrieval_configuration": rule_retrieval.RETRIEVAL_VERSION,
        "model_contract": classification_model.MODEL_CONTRACT_VERSION,
    }

    async def prepare(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        extraction, reconciliation = await classification_inputs(connection, revision)
        if reconciliation != original_reconciliation:
            raise HTTPException(409, "Current source or reconciliation has changed")
        existing = await classification_record(connection, reconciliation)
        if existing is not None and existing["status"] != "failed":
            return ClassificationDetails.model_validate(existing), None

        current_pin = await rule_pin(connection, revision)
        if current_pin != original_pin and (verified is None or current_pin != verified.pin):
            raise HTTPException(409, "The approved rule pin has changed")
        if verified is not None and await rule_pin(connection, revision, infer_legacy=False) is None:
            await save_pin(connection, task_id, revision["revision"], verified.pin)

        attempt = await claim(
            connection, revision, operator, "classification",
            [identity(reconciliation), reconciliation.source_sha256, CLASSIFIER_VERSION, settings.classification_model,
             bool(settings.llm_api_key), rule_identity, rules_error.code if rules_error else None],
            settings.execution_classification_seconds,
        )
        return (extraction, reconciliation), attempt

    inputs, attempt = await acquire(engine, prepare)
    if attempt is None:
        return classification_response(inputs)

    extraction, reconciliation = inputs
    async with executing(engine, attempt):
        skill_sha256 = proposal = supplementary = None
        retrieval = rule_retrieval.RetrievalResult("not_performed")
        model = settings.classification_model
        proposal_id = uuid4()
        try:
            if rules_error is not None:
                raise rules_error
            specification = verified.specification
            skill_sha256 = specification.sha256
            supplementary = specification.supplementary.provenance()
            if supplementary["status"] not in {"applied", "not_configured"}:
                logger.warning("Supplementary classification rules unavailable: %s", supplementary["status"])
            evidence = await to_thread.run_sync(make_evidence, extraction.result, reconciliation.report)
            model_inputs = await to_thread.run_sync(evidence.model_input)
            try:
                retrieval = rule_retrieval.search_rules(
                    specification.text, specification.sha256, primary, search_inputs(model_inputs, reconciliation),
                )
            except Exception as error:
                logger.warning("Approved rule retrieval unavailable: %s", type(error).__name__)
                retrieval = rule_retrieval.RetrievalResult("unavailable")
            specification = replace(specification, retrieved_rules=retrieval.passages)
            async with request.app.state.classification_limiter:
                response = await classification_model.propose(settings, specification, model_inputs)
            try:
                proposal = Proposal.model_validate(response)
            except ValidationError:
                raise ClassificationModelError("invalid_model_proposal") from None
            report = await to_thread.run_sync(build_classification, evidence, proposal, {}, str(proposal_id))
        except (ClassificationEvidenceError, ClassificationModelError) as error:
            logger.warning("Classification unavailable: %s", error.code)
            report = failure_report(error.code)
            proposal = None
        if supplementary is not None:
            report["supplementary_rules"] = supplementary
        primary_verified = verified is not None or (
            rules_error is not None and rules_error.code in PINNED_SUPPLEMENT_ERRORS
        )
        report["rule_context"] = {
            **rule_identity, "retrieval": retrieval.report(),
            "primary_verified": primary_verified,
        }

        async with transaction(engine) as connection:
            revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
            _, latest = await classification_inputs(connection, revision)
            if latest != reconciliation:
                raise HTTPException(409, "Current source or reconciliation has changed")
            if verified is not None and await rule_pin(connection, revision) != verified.pin:
                raise HTTPException(409, "The approved rule pin has changed")
            await fence(connection, attempt)
            current = await classification_record(connection, latest)
            if current is not None and current["status"] != "failed":
                await settle(
                    connection, attempt, "blocked" if current["status"] == "blocked" else "succeeded",
                    result={"status": current["status"], "proposal_id": current["proposal_id"]},
                )
                return ClassificationDetails.model_validate(current)
            result = await save_classification(
                connection, latest, proposal_id, skill_sha256, model, proposal, {}, report,
            )
            code = report.get("error")
            if result.status == "failed":
                status = "retryable" if code in TRANSIENT_CLASSIFICATION_ERRORS else "failed"
            else:
                status = "blocked" if result.status == "blocked" else "succeeded"
            await settle(connection, attempt, status, code, result={
                "status": result.status, "proposal_id": result.proposal_id,
            })
    return classification_response(result, attempt.headers)


def classification_response(result, headers=None):
    if result.status == "failed":
        error = result.report["error"]
        if error in RULE_INPUT_ERRORS:
            if error in EVALUATION_DATA_ERRORS:
                message = "Move evaluation data out of the operational rules"
            elif error in PINNED_SUPPLEMENT_ERRORS:
                message = "The pinned supplementary rules are unavailable"
            else:
                message = "Approved primary rules are unavailable"
            raise HTTPException(503, {"code": error, "message": message}, headers=headers)
        raise HTTPException(503, "Classification unavailable. Read the recorded outcome for details.", headers=headers)
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


def classification_decisions(current, payload, operator):
    question = next(
        (item for item in current["report"]["questions"] if item["id"] == str(payload.question_id)),
        None,
    ) if current else None
    if question is None:
        raise HTTPException(409, "Question is no longer pending on this reconciliation")
    if not question["options"]:
        raise HTTPException(409, "Update the authorized scope in reconciliation")
    if payload.answer not in question["options"] or question["requires_reason"] and not payload.reason:
        raise HTTPException(422, "Select an offered answer and include a reason when required")
    return {
        **current["decisions"],
        question["key"]: {
            "id": question["id"], "kind": question["kind"], "answer": payload.answer,
            "reason": payload.reason, "evidence": question["evidence"], "operator": operator,
        },
    }


def replay_proposal(extraction, reconciliation, proposal, decisions, proposal_id, provenance):
    report = build_classification(make_evidence(extraction, reconciliation), proposal, decisions, str(proposal_id))
    return {**report, **provenance}


@router.post("/answers")
async def answer_classification(
    task_id: UUID, payload: ClassificationAnswer, request: Request, operator: Operator,
    idempotency_key: RequestKey = None,
) -> ClassificationDetails:
    engine = database_for(request)
    reason = payload.reason.strip() if payload.reason else ""
    payload = payload.model_copy(update={"reason": reason or None})
    key = ReceiptKey.from_request(operator, "classification_answer", idempotency_key, [task_id, payload])

    async def snapshot(connection):
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        extraction, reconciliation = await classification_inputs(connection, revision)
        return revision, extraction, reconciliation, await classification_record(connection, reconciliation)

    async def prepare(connection):
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return ClassificationDetails.model_validate(receipt), None
        revision, extraction, reconciliation, current = await snapshot(connection)
        decisions = classification_decisions(current, payload, operator)
        attempt = await claim(
            connection, revision, operator, "classification_answer",
            [identity(reconciliation), reconciliation.source_sha256, CLASSIFIER_VERSION,
             current["proposal_id"], current["report"].get("rule_context"), decisions],
            settings.execution_rules_seconds,
        )
        return (extraction, reconciliation, current, decisions), attempt

    inputs, attempt = await acquire(engine, prepare)
    if attempt is None:
        return inputs
    extraction, reconciliation, prior, decisions = inputs
    proposal = Proposal.from_storage(prior["proposal"])
    provenance = {
        name: prior["report"][name] for name in ("supplementary_rules", "rule_context") if name in prior["report"]
    }
    async with executing(engine, attempt):
        try:
            report = await to_process.run_sync(
                replay_proposal, extraction.result, reconciliation.report, proposal, decisions,
                prior["proposal_id"], provenance, cancellable=True,
            )
        except BrokenWorkerProcess:
            raise ExecutionFailure(503, "Classification unavailable", "classification_worker_unavailable", retryable=True) from None
        except ClassificationEvidenceError as error:
            raise ExecutionFailure(409, "Classification evidence has changed", error.code) from None
        async with transaction(engine) as connection:
            await reserve_owned_receipt(connection, task_id, key)
            _, _, latest, current = await snapshot(connection)
            if latest != reconciliation or current != prior:
                raise HTTPException(409, "Classification inputs or decisions have changed")
            await fence(connection, attempt)
            result = await save_classification(
                connection, latest, prior["proposal_id"], prior["skill_sha256"],
                prior["model"], proposal, decisions, report,
            )
            await save_receipt(connection, key, result)
            await settle(
                connection, attempt, "blocked" if result.status == "blocked" else "succeeded",
                result={"status": result.status, "proposal_id": result.proposal_id},
            )
            return result
