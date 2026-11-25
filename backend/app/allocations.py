"""Run and retrieve allocation results against current owner-approved inputs."""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.allocation import ALLOCATION_VERSION, AllocationPolicy, allocate
from app.allocation_tables import source_allocations
from app.classifications import ClassificationDetails, classification_inputs, classification_record, identity
from app.extractions import ExtractionDetails
from app.operators import Operator
from app.reconcile import amount_text
from app.reconciliations import ReconciliationDetails
from app.tasks import database_for, lock_current_revision, owned_revision, transaction
from app.workflows import RevisionRequest, workflow_details, workflow_record

router = APIRouter(prefix="/tasks/{task_id}/allocation", tags=["allocation"])


class AllocationRequest(RevisionRequest):
    expected_classification_id: UUID
    expected_allocation_id: UUID | None = None
    policy: AllocationPolicy | None = None


class AllocationDetails(BaseModel):
    task_id: UUID
    revision: int
    extractor_version: str
    reconciliation_generation: UUID
    classification_id: UUID
    allocation_id: UUID
    allocator_version: str
    input_sha256: str
    status: Literal["blocked", "ready_for_workbook"]
    operator: str
    policy: AllocationPolicy
    report: dict


@dataclass(frozen=True)
class AllocationInputs:
    extraction: ExtractionDetails
    reconciliation: ReconciliationDetails
    classification: ClassificationDetails
    target: Decimal | None
    gst_basis: str | None
    gst_confirmation_id: UUID | None

    @property
    def key(self):
        return {**identity(self.reconciliation), "classification_id": self.classification.proposal_id}

    def fingerprint(self) -> str:
        evidence = {
            "target": amount_text(self.target), "target_gst_basis": self.gst_basis,
            "gst_confirmation_id": str(self.gst_confirmation_id) if self.gst_confirmation_id else None,
            "reconciliation": self.reconciliation.model_dump(mode="json"),
            "classification": self.classification.model_dump(mode="json"),
        }
        return sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def current_inputs(connection: AsyncConnection, revision) -> AllocationInputs:
    workflow = workflow_details(revision, await workflow_record(connection, revision))
    if revision["target_amount"] is not None and workflow.effective_gst_basis is None:
        raise HTTPException(409, "Confirm the target GST basis in the current revision workflow")
    extraction, reconciliation = await classification_inputs(connection, revision)
    classified = await classification_record(connection, reconciliation)
    if classified is None or classified["status"] != "ready_for_allocation":
        raise HTTPException(409, "Current revision requires a ready classification")
    return AllocationInputs(
        extraction, reconciliation, ClassificationDetails.model_validate(classified), revision["target_amount"],
        workflow.effective_gst_basis, workflow.question.id if workflow.question else None,
    )


async def allocation_record(connection: AsyncConnection, inputs: AllocationInputs, fingerprint: str):
    return (await connection.execute(select(source_allocations).filter_by(
        **inputs.key, allocator_version=ALLOCATION_VERSION, input_sha256=fingerprint,
    ))).mappings().one_or_none()


@router.post("")
async def run_allocation(
    task_id: UUID, payload: AllocationRequest, request: Request, operator: Operator,
) -> AllocationDetails:
    # Revision, reconciliation, classification and policy writers all take this task lock.
    async with transaction(database_for(request)) as connection:
        revision = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        inputs = await current_inputs(connection, revision)
        if payload.expected_classification_id != inputs.classification.proposal_id:
            raise HTTPException(409, "The approved classification has changed")
        fingerprint = inputs.fingerprint()
        recorded = await allocation_record(connection, inputs, fingerprint)
        current = AllocationDetails.model_validate(recorded) if recorded is not None else None
        policy = payload.policy or (current.policy if current else AllocationPolicy())
        if current is not None and current.policy == policy:
            return current
        if payload.expected_allocation_id != (current.allocation_id if current else None):
            raise HTTPException(409, "The allocation approval has changed; reload the current allocation")

        report = await to_thread.run_sync(
            allocate, inputs.reconciliation.report, inputs.classification.report,
            inputs.target, inputs.gst_basis, policy,
        )
        report["source_sha256"] = inputs.reconciliation.source_sha256
        report["classification_skill_sha256"] = inputs.classification.skill_sha256
        report["target_gst_confirmation_id"] = str(inputs.gst_confirmation_id) if inputs.gst_confirmation_id else None
        fields = {
            "allocation_id": uuid4(), "allocator_version": ALLOCATION_VERSION,
            "input_sha256": fingerprint, "operator": operator,
            "status": report["status"], "policy": policy.model_dump(mode="json"), "report": report,
        }
        statement = insert(source_allocations).values(**inputs.key, **fields)
        result = await connection.execute(statement.on_conflict_do_update(
            index_elements=tuple(source_allocations.primary_key.columns), set_=fields,
        ).returning(source_allocations))
        return AllocationDetails.model_validate(result.mappings().one())


@router.get("")
async def get_allocation(task_id: UUID, request: Request, operator: Operator) -> AllocationDetails:
    async with transaction(database_for(request)) as connection:
        revision = await owned_revision(connection, task_id, operator)
        revision = await lock_current_revision(connection, task_id, operator, revision["revision"])
        inputs = await current_inputs(connection, revision)
        record = await allocation_record(connection, inputs, inputs.fingerprint())
    if record is None:
        raise HTTPException(404, "No allocation recorded for the current classification")
    return AllocationDetails.model_validate(record)
