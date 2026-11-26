"""Transport-independent outcomes for application use cases."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class FailureKind(StrEnum):
    PENDING = "pending"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TOO_LARGE = "too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_DATA = "invalid_data"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class Rejection:
    kind: FailureKind
    detail: str | dict | None
    execution_id: UUID | None = None
    retry_after: int | None = None


class UseCaseError(Exception):
    """Roll back the current transaction before returning a rejection."""

    def __init__(self, kind, detail, *, execution_id=None, retry_after=None):
        self.kind = kind
        self.detail = detail
        self.execution_id = execution_id
        self.retry_after = retry_after
        super().__init__(detail)


async def invoke(operation, /, *args, **kwargs):
    try:
        return await operation(*args, **kwargs)
    except UseCaseError as error:
        return Rejection(error.kind, error.detail, error.execution_id, error.retry_after)


@dataclass(frozen=True)
class FileContent:
    data: bytes
    filename: str
    media_type: str
    workbook_status: str | None = None
