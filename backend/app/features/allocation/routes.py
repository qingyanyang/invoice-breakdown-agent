"""HTTP routes for allocation."""

from uuid import UUID

from fastapi import APIRouter, Request

from app.operators import Operator
from app.http import RequestKey, respond
from app.features.allocation import use_cases as actions
from app.features.allocation.use_cases import AllocationDetails, AllocationRequest

router = APIRouter(prefix="/tasks/{task_id}/allocation", tags=["allocation"])


@router.post("")
async def run_allocation(
    task_id: UUID, payload: AllocationRequest, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> AllocationDetails:
    return await respond(actions.run_allocation, task_id, payload, request.app.state, operator, idempotency_key)


@router.get("")
async def get_allocation(task_id: UUID, request: Request, operator: Operator) -> AllocationDetails:
    return await respond(actions.get_allocation, task_id, request.app.state, operator)
