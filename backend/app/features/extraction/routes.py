"""HTTP routes for extraction."""

from uuid import UUID

from fastapi import APIRouter, Request

from app.operators import Operator
from app.http import respond
from app.features.extraction import use_cases as actions
from app.features.tasks.models import RevisionRequest
from app.features.extraction.use_cases import ExtractionDetails

router = APIRouter(prefix="/tasks/{task_id}/extraction", tags=["extraction"])


@router.post("")
async def run_extraction(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> ExtractionDetails:
    return await respond(actions.run_extraction, task_id, payload, request.app.state, operator)


@router.get("")
async def get_extraction(task_id: UUID, request: Request, operator: Operator) -> ExtractionDetails:
    return await respond(actions.get_extraction, task_id, request.app.state, operator)
