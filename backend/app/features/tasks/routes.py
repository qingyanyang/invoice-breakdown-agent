"""HTTP routes for tasks."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.operators import Operator
from app.features.tasks.upload import XLSX_MEDIA_TYPE
from app.http import RequestKey, respond, upload_chunks
from app.features.tasks import use_cases as actions
from app.features.tasks.models import CreateTask, TaskDetails, UpdateTarget

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", status_code=201)
async def create_task(
    payload: CreateTask, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> TaskDetails:
    return await respond(actions.create_task, payload, request.app.state, operator, idempotency_key)


@router.get("/{task_id}")
async def read_task(task_id: UUID, request: Request, operator: Operator) -> TaskDetails:
    return await respond(actions.read_task, task_id, request.app.state, operator)


@router.put("/{task_id}/target")
async def change_target(
    task_id: UUID, payload: UpdateTarget, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> TaskDetails:
    return await respond(actions.change_target, task_id, payload, request.app.state, operator, idempotency_key)


@router.put(
    "/{task_id}/source",
    openapi_extra={"requestBody": {"required": True, "content": {
        XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
    }}},
)
async def upload_source(
    task_id: UUID, request: Request, operator: Operator,
    expected_revision: Annotated[int, Query(ge=1)], filename: str, idempotency_key: RequestKey = None,
) -> TaskDetails:
    return await respond(
        actions.upload_source, task_id, request.app.state, operator, expected_revision, filename,
        idempotency_key, chunks=upload_chunks(request),
    )


@router.get("/{task_id}/source", response_class=Response)
async def read_source(
    task_id: UUID, request: Request, operator: Operator,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    return await respond(actions.read_source, task_id, request.app.state, operator, revision)
