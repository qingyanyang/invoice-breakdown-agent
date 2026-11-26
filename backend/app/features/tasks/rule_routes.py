"""HTTP routes for approved rules."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.operators import Operator
from app.http import RequestKey, respond
from app.features.tasks import rules as actions
from app.features.tasks.rules import RulePin, RuleUpgrade, UpgradeReceipt

router = APIRouter(prefix="/tasks/{task_id}/rules", tags=["approved rules"])


@router.get("")
async def read_rules(
    task_id: UUID, request: Request, operator: Operator,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> RulePin | None:
    return await respond(actions.read_rules, task_id, request.app.state, operator, revision)


@router.post("/upgrade")
async def upgrade_rules(
    task_id: UUID, payload: RuleUpgrade, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> UpgradeReceipt:
    return await respond(actions.upgrade_rules, task_id, payload, request.app.state, operator, idempotency_key)
