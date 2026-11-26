"""HTTP routes for workflow."""

from uuid import UUID

from fastapi import APIRouter, Request

from app.operators import Operator
from app.http import RequestKey, respond
from app.workflows import gst as actions
from app.features.tasks.models import RevisionRequest
from app.workflows.gst import AnswerRequest, WorkflowDetails

router = APIRouter(prefix="/tasks/{task_id}/workflow", tags=["workflow"])


@router.get("")
async def get_workflow(task_id: UUID, request: Request, operator: Operator) -> WorkflowDetails:
    return await respond(actions.get_workflow, task_id, request.app.state, operator)


@router.post("")
async def start_or_recover(
    task_id: UUID, payload: RevisionRequest, request: Request, operator: Operator,
) -> WorkflowDetails:
    return await respond(actions.start_or_recover, task_id, payload, request.app.state, operator)


@router.post("/answers")
async def answer_question(
    task_id: UUID, payload: AnswerRequest, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> WorkflowDetails:
    return await respond(actions.answer_question, task_id, payload, request.app.state, operator, idempotency_key)
