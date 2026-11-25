"""Claim and fence short-lived attempts for an immutable set of task inputs."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from pathlib import PurePosixPath
from time import monotonic
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.execution_tables import execution_objects, execution_runs
from app.request_receipts import fingerprint

logger = logging.getLogger(__name__)


@asynccontextmanager
async def transaction(engine):
    try:
        with fail_after(settings.execution_db_timeout_seconds + 2):
            async with engine.begin() as connection:
                timeout = f"{settings.execution_db_timeout_seconds}s"
                await connection.execute(select(
                    func.set_config("statement_timeout", timeout, True),
                    func.set_config("lock_timeout", timeout, True),
                ))
                yield connection
    except (SQLAlchemyError, TimeoutError) as error:
        logger.warning("Task database operation failed: %s", type(error).__name__)
        raise HTTPException(503, "Task database unavailable") from None


class ExecutionFailure(HTTPException):
    def __init__(self, status: int, detail, code: str, *, retryable: bool = False):
        super().__init__(status, detail)
        self.code = code
        self.retryable = retryable


class StaleAttempt(HTTPException):
    def __init__(self, attempt):
        super().__init__(409, "Execution attempt is no longer current", headers=attempt.headers)


@dataclass(frozen=True)
class Attempt:
    id: UUID
    number: int
    token: UUID
    monotonic_deadline: float

    @property
    def remaining_seconds(self):
        return max(0.0, self.monotonic_deadline - monotonic())

    @property
    def filters(self):
        return (
            execution_runs.c.id == self.id,
            execution_runs.c.attempt == self.number,
            execution_runs.c.worker_token == self.token,
            execution_runs.c.status == "running",
        )

    @property
    def headers(self):
        return {"X-Execution-Id": str(self.id)}


@dataclass(frozen=True)
class Unavailable:
    id: UUID
    status: str
    seconds: float | None = None
    failure_code: str | None = None
    exhaustion_reason: str | None = None

    def error(self):
        headers = {"X-Execution-Id": str(self.id)}
        status_code = 409
        if self.status in {"running", "retryable"}:
            status_code = 202
            headers["Retry-After"] = str(max(1, ceil(self.seconds or 0)))
        if self.status == "exhausted":
            status_code = 503
        detail = {"execution_id": str(self.id), "status": self.status, "failure_code": self.failure_code}
        if self.exhaustion_reason is not None:
            detail["exhaustion_reason"] = self.exhaustion_reason
        return HTTPException(status_code, detail, headers=headers)


async def claim(connection, revision, operator, operation, inputs, seconds) -> Attempt | Unavailable:
    key = {
        "task_id": revision["task_id"], "revision": revision["revision"], "operator": operator,
        "operation": operation, "input_sha256": fingerprint(inputs),
    }
    clock = func.clock_timestamp()
    await connection.execute(insert(execution_runs).values(
        **key, id=uuid4(), status="retryable", attempt=0, max_attempts=settings.execution_max_attempts,
        deadline=clock + timedelta(seconds=settings.execution_deadline_seconds),
    ).on_conflict_do_nothing(constraint="uq_execution_inputs"))
    row = (await connection.execute(select(execution_runs).filter_by(**key).with_for_update())).mappings().one()
    now = await connection.scalar(select(clock))
    if row["status"] not in {"running", "retryable"}:
        return Unavailable(
            row["id"], row["status"], failure_code=row["failure_code"], exhaustion_reason=row["exhaustion_reason"],
        )
    if row["status"] == "running" and row["lease_expires_at"] > now:
        return Unavailable(row["id"], "running", (row["lease_expires_at"] - now).total_seconds())
    if row["deadline"] <= now or row["attempt"] >= row["max_attempts"]:
        reason = "deadline" if row["deadline"] <= now else "attempt_limit"
        failure = row["failure_code"] or "lease_expired"
        await connection.execute(update(execution_runs).filter_by(id=row["id"]).values(
            status="exhausted", failure_code=failure, exhaustion_reason=reason, updated_at=now,
        ))
        return Unavailable(row["id"], "exhausted", failure_code=failure, exhaustion_reason=reason)
    if row["retry_at"] and row["retry_at"] > now:
        return Unavailable(row["id"], "retryable", (row["retry_at"] - now).total_seconds(), row["failure_code"])

    deadline = min(row["deadline"], now + timedelta(seconds=seconds))
    attempt = Attempt(row["id"], row["attempt"] + 1, uuid4(), monotonic() + (deadline - now).total_seconds())
    await connection.execute(update(execution_runs).filter_by(id=row["id"]).values(
        status="running", attempt=attempt.number, worker_token=attempt.token, attempt_expires_at=deadline,
        lease_expires_at=min(deadline, now + timedelta(seconds=settings.execution_lease_seconds)),
        retry_at=None, updated_at=now,
    ))
    return attempt


async def acquire(engine, prepare):
    """Wait briefly for a duplicate or a scheduled retry without holding any locks."""
    loop = asyncio.get_running_loop()
    wait_until = loop.time() + settings.execution_duplicate_wait_seconds
    while True:
        async with transaction(engine) as connection:
            inputs, attempt = await prepare(connection)
        if not isinstance(attempt, Unavailable):
            return inputs, attempt
        remaining = wait_until - loop.time()
        if attempt.status not in {"running", "retryable"} or remaining <= 0:
            raise attempt.error()
        await asyncio.sleep(min(remaining, 0.05 if attempt.status == "running" else attempt.seconds or 0.05))


async def fence(connection, attempt: Attempt):
    row = (await connection.execute(
        select(execution_runs).where(*attempt.filters).with_for_update(),
    )).mappings().one_or_none()
    now = await connection.scalar(select(func.clock_timestamp()))
    if row is None or min(row["lease_expires_at"], row["attempt_expires_at"]) <= now:
        raise StaleAttempt(attempt)
    return row


async def settle(connection, attempt: Attempt, status: str, failure_code: str | None = None, *, result=None):
    row = await fence(connection, attempt)
    return await finish(connection, attempt, row, status, failure_code, result=result)


async def finish(connection, attempt, row, status, failure_code, *, result=None):
    now = await connection.scalar(select(func.clock_timestamp()))
    retry_at = exhaustion_reason = None
    if status == "retryable":
        delay = settings.execution_retry_base_seconds * 2 ** (attempt.number - 1)
        retry_at = now + timedelta(seconds=delay)
        if attempt.number >= row["max_attempts"]:
            exhaustion_reason = "attempt_limit"
        elif retry_at >= row["deadline"]:
            exhaustion_reason = "deadline"
        if exhaustion_reason is not None:
            status, retry_at = "exhausted", None
    await connection.execute(update(execution_runs).where(*attempt.filters).values(
        status=status, failure_code=failure_code, exhaustion_reason=exhaustion_reason,
        result=jsonable_encoder(result), retry_at=retry_at, updated_at=now,
        worker_token=None, lease_expires_at=None,
    ))
    return status


async def heartbeat(engine, attempt):
    interval = settings.execution_lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        try:
            async with transaction(engine) as connection:
                clock = func.clock_timestamp()
                renewed = await connection.scalar(update(execution_runs).where(
                    *attempt.filters, execution_runs.c.lease_expires_at > clock,
                    execution_runs.c.attempt_expires_at > clock,
                ).values(
                    lease_expires_at=func.least(
                        execution_runs.c.attempt_expires_at, clock + timedelta(seconds=settings.execution_lease_seconds),
                    ),
                ).returning(execution_runs.c.id))
                if renewed is None:
                    return
        except HTTPException:
            # An unavailable database cannot extend or transfer an attempt's authority.
            return


async def record_failure(engine, attempt, status, code):
    try:
        with CancelScope(shield=True):
            async with transaction(engine) as connection:
                row = (await connection.execute(
                    select(execution_runs).where(*attempt.filters).with_for_update(),
                )).mappings().one_or_none()
                if row is not None:
                    return await finish(connection, attempt, row, status, code)
    except HTTPException:
        # A superseded worker has no right to change the winning result.
        return None


@asynccontextmanager
async def executing(engine, attempt: Attempt):
    renewal = asyncio.create_task(heartbeat(engine, attempt))
    try:
        with fail_after(attempt.remaining_seconds):
            yield
    except TimeoutError:
        await record_failure(engine, attempt, "retryable", "attempt_deadline")
        raise HTTPException(504, "Execution attempt exceeded its time limit", headers=attempt.headers) from None
    except ExecutionFailure as error:
        await record_failure(engine, attempt, "retryable" if error.retryable else "failed", error.code)
        error.headers = attempt.headers
        raise
    except SQLAlchemyError as error:
        logger.warning("Execution database operation failed: %s", type(error).__name__)
        await record_failure(engine, attempt, "retryable", "database_unavailable")
        raise HTTPException(503, "Task database unavailable", headers=attempt.headers) from None
    except StaleAttempt:
        raise
    except HTTPException as error:
        error.headers = {**(error.headers or {}), **attempt.headers}
        if error.status_code in {409, 503}:
            await record_failure(
                engine, attempt, "retryable" if error.status_code == 503 else "failed",
                "database_or_dependency_unavailable" if error.status_code == 503 else "inputs_changed",
            )
        raise
    except Exception as error:
        logger.error("Execution failed unexpectedly: %s", type(error).__name__)
        await record_failure(engine, attempt, "failed", "execution_error")
        raise HTTPException(500, "Execution failed", headers=attempt.headers) from None
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal


async def register_object(engine, attempt: Attempt, key: str):
    async with transaction(engine) as connection:
        await fence(connection, attempt)
        await connection.execute(insert(execution_objects).values(
            storage_key=key, execution_id=attempt.id, attempt=attempt.number,
        ))


async def forget_colliding_object(engine, attempt: Attempt, key: str):
    # An overwrite-free collision means this attempt did not create the object.
    async with transaction(engine) as connection:
        await connection.execute(delete(execution_objects).where(
            execution_objects.c.storage_key == key,
            execution_objects.c.execution_id == attempt.id,
            execution_objects.c.attempt == attempt.number,
        ))


def final_object_key(key: str) -> str:
    path = PurePosixPath(key)
    if path.name.startswith(".") and path.suffix == ".staging":
        stem = path.name.split(".")[1]
        extension = ".pdf" if stem == "preview" else ".xlsx"
        return str(path.with_name(stem + extension))
    return key


async def object_has_live_attempt(connection, key: str) -> bool:
    clock = func.clock_timestamp()
    current = await connection.scalar(select(execution_runs.c.id).join(
        execution_objects, execution_objects.c.execution_id == execution_runs.c.id,
    ).where(
        execution_objects.c.storage_key == key,
        execution_runs.c.attempt == execution_objects.c.attempt,
        execution_runs.c.status == "running",
        execution_runs.c.lease_expires_at > clock,
        execution_runs.c.attempt_expires_at > clock,
    ))
    return current is not None
