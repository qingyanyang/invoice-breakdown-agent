"""HTTP routes for reconciliation."""

from uuid import UUID

from fastapi import APIRouter, Request

from app.operators import Operator
from app.http import RequestKey, respond
from app.features.reconciliation import use_cases as actions
from app.features.reconciliation.use_cases import ReconcileRequest, ReconciliationAnswer, ReconciliationDetails

router = APIRouter(prefix="/tasks/{task_id}/reconciliation", tags=["reconciliation"])


@router.post("")
async def run_reconciliation(
    task_id: UUID, payload: ReconcileRequest, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> ReconciliationDetails:
    return await respond(
        actions.run_reconciliation, task_id, payload, request.app.state, operator, idempotency_key,
    )


@router.get("")
async def get_reconciliation(task_id: UUID, request: Request, operator: Operator) -> ReconciliationDetails:
    return await respond(actions.get_reconciliation, task_id, request.app.state, operator)


@router.post("/answers")
async def answer_reconciliation(
    task_id: UUID, payload: ReconciliationAnswer, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> ReconciliationDetails:
    return await respond(
        actions.answer_reconciliation, task_id, payload, request.app.state, operator, idempotency_key,
    )
