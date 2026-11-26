"""HTTP routes for workbook."""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.operators import Operator
from app.http import respond
from app.features.workbooks import use_cases as actions
from app.features.workbooks.use_cases import WorkbookDetails, WorkbookRequest

router = APIRouter(prefix="/tasks/{task_id}/workbook", tags=["workbook"])


@router.post("")
async def create_workbook(task_id: UUID, payload: WorkbookRequest, request: Request, operator: Operator) -> WorkbookDetails:
    return await respond(actions.create_workbook, task_id, payload, request.app.state, operator)


@router.get("")
async def get_workbook(task_id: UUID, request: Request, operator: Operator) -> WorkbookDetails:
    return await respond(actions.get_workbook, task_id, request.app.state, operator)


@router.get("/download", response_class=Response)
async def download_workbook(task_id: UUID, request: Request, operator: Operator) -> Response:
    return await respond(actions.download_workbook, task_id, request.app.state, operator)
