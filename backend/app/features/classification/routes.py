"""HTTP routes for classification."""

from uuid import UUID

from fastapi import APIRouter, Request

from app.operators import Operator
from app.http import RequestKey, respond
from app.features.classification import use_cases as actions
from app.features.tasks.models import RevisionRequest
from app.features.classification.use_cases import ClassificationAnswer, ClassificationDetails

router = APIRouter(prefix="/tasks/{task_id}/classification", tags=["classification"])


@router.post("")
async def run_classification(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> ClassificationDetails:
    return await respond(actions.run_classification, task_id, payload, request.app.state, operator)


@router.get("")
async def get_classification(task_id: UUID, request: Request, operator: Operator) -> ClassificationDetails:
    return await respond(actions.get_classification, task_id, request.app.state, operator)


@router.post("/answers")
async def answer_classification(
    task_id: UUID, payload: ClassificationAnswer, request: Request, operator: Operator,
    idempotency_key: RequestKey = None,
) -> ClassificationDetails:
    return await respond(
        actions.answer_classification, task_id, payload, request.app.state, operator, idempotency_key,
    )
