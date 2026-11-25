"""Read execution outcomes for an authorized task revision."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.execution import effective_state
from app.execution_tables import execution_runs
from app.operators import Operator
from app.tasks import database_for, owned_revision, transaction

router = APIRouter(prefix="/tasks/{task_id}/executions", tags=["execution reliability"])


@router.get("")
async def read_executions(
    task_id: UUID, request: Request, operator: Operator, revision: Annotated[int | None, Query(ge=1)] = None,
):
    async with transaction(database_for(request)) as connection:
        current = await owned_revision(connection, task_id, operator, revision=revision)
        result = await connection.execute(select(
            execution_runs.c.id, execution_runs.c.operation, execution_runs.c.revision,
            execution_runs.c.status, execution_runs.c.attempt, execution_runs.c.max_attempts,
            execution_runs.c.lease_expires_at, execution_runs.c.attempt_expires_at, execution_runs.c.deadline,
            execution_runs.c.retry_at, execution_runs.c.failure_code, execution_runs.c.exhaustion_reason,
            execution_runs.c.result, execution_runs.c.updated_at,
        ).where(
            execution_runs.c.task_id == task_id, execution_runs.c.revision == current["revision"],
            execution_runs.c.operator == operator,
        ).order_by(execution_runs.c.updated_at.desc(), execution_runs.c.id).limit(50))
        now = await connection.scalar(select(func.clock_timestamp()))
        history = []
        for row in result.mappings():
            state = effective_state(row, now)
            history.append(dict(row) | {
                "status": state.status, "retry_at": state.retry_at,
                "failure_code": state.failure_code, "exhaustion_reason": state.exhaustion_reason,
            })
        return history
