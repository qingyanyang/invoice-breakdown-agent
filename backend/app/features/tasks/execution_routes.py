"""HTTP routes for execution reliability."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.operators import Operator
from app.http import respond
from app.features.tasks import executions as actions

router = APIRouter(prefix="/tasks/{task_id}/executions", tags=["execution reliability"])


@router.get("")
async def read_executions(
    task_id: UUID, request: Request, operator: Operator, revision: Annotated[int | None, Query(ge=1)] = None,
):
    return await respond(actions.read_executions, task_id, request.app.state, operator, revision)
