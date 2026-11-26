"""HTTP routes for workbook QA and delivery."""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.operators import Operator
from app.http import RequestKey, respond
from app.features.qa import release
from app.features.qa import use_cases as actions
from app.features.qa.release import VisualReview
from app.features.qa.use_cases import QARequest

router = APIRouter(prefix="/tasks/{task_id}/delivery", tags=["workbook QA and delivery"])


async def qa_response(operation, task_id, *args):
    result = await respond(operation, task_id, *args)
    if "delivery_summary" in result:
        summary = {**result["delivery_summary"], "文件": f"/tasks/{task_id}/delivery/download"}
        return {**result, "delivery_summary": summary}
    return result


@router.post("/qa")
async def start_qa(task_id: UUID, payload: QARequest, request: Request, operator: Operator):
    return await qa_response(actions.start_qa, task_id, payload, request.app.state, operator)


@router.get("/qa")
async def read_qa(task_id: UUID, request: Request, operator: Operator):
    return await qa_response(release.read_qa, task_id, request.app.state, operator)


@router.post("/qa/visual-review")
async def accept_visual_review(
    task_id: UUID, payload: VisualReview, request: Request, operator: Operator, idempotency_key: RequestKey = None,
):
    return await qa_response(
        release.accept_visual_review, task_id, payload, request.app.state, operator, idempotency_key,
    )


@router.get("/qa/preview", response_class=Response)
async def download_preview(task_id: UUID, request: Request, operator: Operator):
    return await respond(release.download_preview, task_id, request.app.state, operator)


@router.get("/download", response_class=Response)
async def download_final(task_id: UUID, request: Request, operator: Operator):
    return await respond(release.download_final, task_id, request.app.state, operator)
