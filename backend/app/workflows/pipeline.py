"""Advance the existing stages for a single authorized task revision."""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from app.features.allocation import use_cases as allocations
from app.features.classification import use_cases as classifications
from app.features.extraction import use_cases as extractions
from app.features.qa import use_cases as qa
from app.features.reconciliation import use_cases as reconciliations
from app.features.tasks.models import RevisionRequest
from app.features.workbooks import use_cases as workbooks
from app.outcomes import FailureKind, Rejection, invoke
from app.workflows import gst


class StageRejected(Exception):
    """A failed stage or one whose execution is still pending."""

    def __init__(self, stage, rejection: Rejection):
        self.stage = stage
        self.rejection = rejection
        super().__init__(stage)


@dataclass(frozen=True)
class Progress:
    stage: str
    result: dict


class InvoiceWorkflow:
    def __init__(self, resources, operator: str):
        self.resources = resources
        self.operator = operator

    async def run(self, stage, operation, *inputs, **options):
        """Invoke a stage with the same identity and resources as its HTTP route."""
        result = await invoke(operation, *inputs, self.resources, self.operator, **options)
        if isinstance(result, Rejection):
            raise StageRejected(stage, result)
        if isinstance(result, (BaseModel, dict)):
            return to_jsonable_python(result, by_alias=True)
        return result

    async def submit(self, stage, operation, command, task_id, payload, *, idempotency_key=None):
        try:
            validated = command.model_validate(payload)
        except ValidationError:
            raise StageRejected(stage, Rejection(FailureKind.INVALID_DATA, None)) from None
        options = {"idempotency_key": idempotency_key} if idempotency_key is not None else {}
        return await self.run(stage, operation, task_id, validated, **options)

    async def advance(self, task_id, revision: int, key) -> Progress:
        current = RevisionRequest(expected_revision=revision)
        workflow = await self.run("workflow", gst.start_or_recover, task_id, current)
        if workflow["status"] != "ready_for_extraction":
            return Progress("workflow", workflow)
        await self.run("extraction", extractions.run_extraction, task_id, current)
        reconciliation = await self.run(
            "reconciliation", reconciliations.run_reconciliation, task_id,
            reconciliations.ReconcileRequest(expected_revision=revision), idempotency_key=key("reconciliation", {}),
        )
        if reconciliation["status"] != "ready_for_classification":
            return Progress("reconciliation", reconciliation)
        classified = await self.run("classification", classifications.run_classification, task_id, current)
        if classified["status"] != "ready_for_allocation":
            return Progress("classification", classified)
        try:
            allocation = await self.run("allocation", allocations.get_allocation, task_id)
        except StageRejected as error:
            if error.rejection.kind != FailureKind.NOT_FOUND:
                raise
            inputs = {"expected_classification_id": classified["proposal_id"]}
            allocation = await self.submit(
                "allocation", allocations.run_allocation, allocations.AllocationRequest,
                task_id, {"expected_revision": revision, **inputs}, idempotency_key=key("allocation", inputs),
            )
        if allocation["status"] != "ready_for_workbook":
            return Progress("allocation", allocation)
        workbook = await self.submit(
            "workbook", workbooks.create_workbook, workbooks.WorkbookRequest, task_id,
            {"expected_revision": revision, "expected_allocation_id": allocation["allocation_id"]},
        )
        result = await self.submit(
            "qa", qa.start_qa, qa.QARequest, task_id,
            {"expected_revision": revision, "expected_artifact_id": workbook["artifact_id"]},
        )
        return Progress("qa", result)

    async def answer(self, stage: str, task_id, payload, idempotency_key: str):
        commands = {
            "workflow": (gst.answer_question, gst.AnswerRequest),
            "reconciliation": (reconciliations.answer_reconciliation, reconciliations.ReconciliationAnswer),
            "classification": (classifications.answer_classification, classifications.ClassificationAnswer),
        }
        operation, command = commands[stage]
        return await self.submit(
            "answers", operation, command, task_id, payload, idempotency_key=idempotency_key,
        )
