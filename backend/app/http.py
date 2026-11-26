"""FastAPI mapping for backend use-case results."""

from typing import Annotated
from urllib.parse import quote

from fastapi import Header, HTTPException, Response
from starlette.requests import ClientDisconnect

from app.outcomes import FailureKind, FileContent, Rejection, UseCaseError, invoke

RequestKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]

HTTP_STATUS = {
    FailureKind.PENDING: 202,
    FailureKind.INVALID_REQUEST: 400,
    FailureKind.NOT_FOUND: 404,
    FailureKind.CONFLICT: 409,
    FailureKind.TOO_LARGE: 413,
    FailureKind.UNSUPPORTED_FORMAT: 415,
    FailureKind.INVALID_DATA: 422,
    FailureKind.FAILED: 500,
    FailureKind.UNAVAILABLE: 503,
    FailureKind.TIMED_OUT: 504,
}


async def respond(operation, /, *args, **kwargs):
    result = await invoke(operation, *args, **kwargs)
    if isinstance(result, Rejection):
        headers = {}
        if result.execution_id is not None:
            headers["X-Execution-Id"] = str(result.execution_id)
        if result.retry_after is not None:
            headers["Retry-After"] = str(result.retry_after)
        raise HTTPException(HTTP_STATUS[result.kind], result.detail, headers=headers or None)
    if isinstance(result, FileContent):
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(result.filename, safe=""),
            "Cache-Control": "private, no-store",
        }
        if result.workbook_status is not None:
            headers["X-Workbook-Status"] = result.workbook_status
        return Response(result.data, media_type=result.media_type, headers=headers)
    return result


def http_error(error: UseCaseError) -> HTTPException:
    return HTTPException(HTTP_STATUS[error.kind], error.detail)


async def upload_chunks(request):
    try:
        async for chunk in request.stream():
            yield chunk
    except ClientDisconnect:
        raise UseCaseError(FailureKind.INVALID_REQUEST, "Workbook upload was interrupted") from None
