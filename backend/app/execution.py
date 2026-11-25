"""Claim and fence short-lived attempts for an immutable set of task inputs."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from pathlib import PurePosixPath
from time import monotonic
from uuid import UUID, uuid4

from anyio import CancelScope, fail_after
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.execution_tables import execution_objects, execution_runs
from app.request_receipts import fingerprint
from app.source_storage import write_with_outcome

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
    def live_filters(self):
        clock = func.clock_timestamp()
        return (
            *self.filters,
            execution_runs.c.lease_expires_at > clock,
            execution_runs.c.attempt_expires_at > clock,
            execution_runs.c.deadline > clock,
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


@dataclass(frozen=True)
class ExecutionState:
    status: str
    failure_code: str | None
    exhaustion_reason: str | None = None
    retry_at: datetime | None = None


def active_until(row):
    return min(row["lease_expires_at"], row["attempt_expires_at"], row["deadline"])


def retry_delay(attempt_number):
    return timedelta(seconds=settings.execution_retry_base_seconds * 2 ** (attempt_number - 1))


def effective_state(row, now) -> ExecutionState:
    """Evaluate claim eligibility and historical status against a PostgreSQL timestamp."""
    status, failure = row["status"], row["failure_code"]
    if status not in {"running", "retryable"}:
        return ExecutionState(status, failure, row["exhaustion_reason"], row["retry_at"])

    retry_at = row["retry_at"]
    if status == "running":
        cutoff = active_until(row)
        if now < cutoff:
            return ExecutionState(status, failure)
        if cutoff == row["attempt_expires_at"]:
            failure = "attempt_deadline"
            retry_at = cutoff + retry_delay(row["attempt"])
        elif cutoff == row["lease_expires_at"]:
            failure = "lease_expired"
        else:
            failure = "execution_deadline"

    if now >= row["deadline"] or (retry_at is not None and retry_at >= row["deadline"]):
        return ExecutionState("exhausted", failure, "deadline")
    if row["attempt"] >= row["max_attempts"]:
        return ExecutionState("exhausted", failure, "attempt_limit")
    return ExecutionState("retryable", failure, retry_at=retry_at)


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
    state = effective_state(row, now)
    if state.status == "running":
        return Unavailable(row["id"], "running", (active_until(row) - now).total_seconds())
    if state.status == "exhausted" and row["status"] != "exhausted":
        await connection.execute(update(execution_runs).filter_by(id=row["id"]).values(
            status="exhausted", failure_code=state.failure_code, exhaustion_reason=state.exhaustion_reason,
            worker_token=None, lease_expires_at=None, retry_at=None, updated_at=now,
        ))
    if state.status != "retryable":
        return Unavailable(
            row["id"], state.status, failure_code=state.failure_code, exhaustion_reason=state.exhaustion_reason,
        )
    if state.retry_at and state.retry_at > now:
        return Unavailable(row["id"], state.status, (state.retry_at - now).total_seconds(), state.failure_code)

    deadline = min(row["deadline"], now + timedelta(seconds=seconds))
    attempt = Attempt(row["id"], row["attempt"] + 1, uuid4(), monotonic() + (deadline - now).total_seconds())
    await connection.execute(update(execution_runs).filter_by(id=row["id"]).values(
        status="running", attempt=attempt.number, worker_token=attempt.token, attempt_expires_at=deadline,
        lease_expires_at=min(deadline, now + timedelta(seconds=settings.execution_lease_seconds)),
        retry_at=None, failure_code=state.failure_code, updated_at=now,
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
    if row is None or active_until(row) <= now:
        raise StaleAttempt(attempt)
    return row, now


async def settle(connection, attempt: Attempt, status: str, failure_code: str | None = None, *, result=None):
    row, now = await fence(connection, attempt)
    retry_at = exhaustion_reason = None
    if status == "retryable":
        retry_at = now + retry_delay(attempt.number)
        if attempt.number >= row["max_attempts"]:
            exhaustion_reason = "attempt_limit"
        elif retry_at >= row["deadline"]:
            exhaustion_reason = "deadline"
        if exhaustion_reason is not None:
            status, retry_at = "exhausted", None
    settled = await connection.scalar(update(execution_runs).where(*attempt.live_filters).values(
        status=status, failure_code=failure_code, exhaustion_reason=exhaustion_reason,
        result=jsonable_encoder(result), retry_at=retry_at, updated_at=now,
        worker_token=None, lease_expires_at=None,
    ).returning(execution_runs.c.id))
    if settled is None:
        raise StaleAttempt(attempt)


async def heartbeat(engine, attempt):
    interval = settings.execution_lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        try:
            async with transaction(engine) as connection:
                row, now = await fence(connection, attempt)
                renewed = await connection.scalar(update(execution_runs).where(*attempt.live_filters).values(
                    lease_expires_at=min(
                        row["attempt_expires_at"], row["deadline"], now + timedelta(seconds=settings.execution_lease_seconds),
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
                await settle(connection, attempt, status, code)
    except HTTPException:
        # Expired attempts cannot settle, even when no replacement has claimed the run yet.
        # Claim and history evaluate abandoned leases and elapsed attempt deadlines.
        pass


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
            storage_key=key, execution_id=attempt.id, attempt=attempt.number, write_state="pending",
        ))


async def write_registered_object(engine, attempt: Attempt, worker, storage, key: str, data: bytes):
    async def record_outcome(outcome):
        # This belongs to one immutable storage reservation. It can finish after
        # the request and does not grant authority to publish an application result.
        async with transaction(engine) as connection:
            await connection.execute(update(execution_objects).where(
                execution_objects.c.storage_key == key,
                execution_objects.c.execution_id == attempt.id,
                execution_objects.c.attempt == attempt.number,
                execution_objects.c.write_state == "pending",
            ).values(write_state=outcome.state))

    outcome = await worker.run(write_with_outcome, storage, key, data, write_key=key, complete=record_outcome)
    if outcome.error is not None:
        raise outcome.error


def final_object_key(key: str) -> str:
    path = PurePosixPath(key)
    if path.name.startswith(".") and path.suffix == ".staging":
        stem = path.name.split(".")[1]
        extension = ".pdf" if stem == "preview" else ".xlsx"
        return str(path.with_name(stem + extension))
    return key


async def object_may_be_discarded(connection, key: str) -> bool:
    final_key = final_object_key(key)
    # Serialize the ownership decision with lease renewal and publication.
    record = (await connection.execute(select(
        execution_objects.c.write_state, execution_objects.c.attempt.label("object_attempt"),
        execution_runs.c.attempt, execution_runs.c.status,
        execution_runs.c.lease_expires_at, execution_runs.c.attempt_expires_at, execution_runs.c.deadline,
    ).join(
        execution_runs, execution_objects.c.execution_id == execution_runs.c.id,
    ).where(execution_objects.c.storage_key == final_key).with_for_update(of=execution_runs))).mappings().one_or_none()
    if record is None:
        return True
    if record["status"] == "running" and record["attempt"] == record["object_attempt"]:
        now = await connection.scalar(select(func.clock_timestamp()))
        if active_until(record) > now:
            return False
    # A staging name is unique to the temporary file. A final name might
    # have collided, so a reservation alone never authorizes its deletion.
    return key != final_key or record["write_state"] == "written"
