"""Verify approved rules, maintain task pins, and authorize explicit upgrades."""

import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Annotated
from uuid import UUID

from anyio import fail_after
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from app import classification_model
from app.classification_tables import source_classifications
from app.config import Settings, settings
from app.operators import Operator
from app.request_receipts import ReceiptKey, RequestKey, save_receipt
from app.rule_retrieval import RuleDocument
from app.task_tables import task_rule_pins
from app.tasks import (
    TaskDetails, append_revision, database_for, lock_current_revision, owned_revision, reserve_owned_receipt, transaction,
)
from app.workflows import RevisionRequest

router = APIRouter(prefix="/tasks/{task_id}/rules", tags=["approved rules"])


@dataclass(frozen=True)
class RulePin:
    primary: RuleDocument
    supplementary: RuleDocument | None = None

    def report(self):
        return asdict(self)


@dataclass(frozen=True)
class VerifiedRules:
    pin: RulePin
    specification: classification_model.PrimarySpecification


def primary_document(digest: str, version: str | None = None) -> RuleDocument:
    return RuleDocument("SKILL.md", version or f"sha256:{digest}", digest)


def applied_supplementary(specification) -> RuleDocument | None:
    facts = specification.supplementary.provenance()
    if facts["status"] != "applied":
        return None
    return RuleDocument("Invoice.Breakdown.Codex.md", facts["version"], facts["sha256"])


def read_verified_rules(config: Settings, pin: RulePin | None) -> VerifiedRules:
    expected = pin.primary if pin else primary_document(config.classification_skill_sha256, config.classification_skill_version)
    paths = [config.classification_skill_path]
    if pin and config.classification_rules_archive and re.fullmatch(r"[0-9a-f]{64}", expected.sha256):
        paths.append(Path(config.classification_rules_archive) / expected.sha256 / expected.document)
    mismatch = False
    for path in paths:
        try:
            specification = classification_model.read_primary_specification(path, config.classification_codex_path)
        except classification_model.ClassificationModelError:
            continue
        if specification.sha256 != expected.sha256:
            mismatch = True
            continue

        supplementary = applied_supplementary(specification)
        if pin is None:
            selected = RulePin(expected, supplementary)
        else:
            selected = pin
            if supplementary is not None and supplementary != pin.supplementary:
                specification = replace(
                    specification, supplementary=classification_model.SupplementarySpecification("version_not_pinned"),
                )
        return VerifiedRules(selected, specification)

    code = "primary_specification_mismatch" if mismatch else "primary_specification_unavailable"
    raise classification_model.ClassificationModelError(code)


async def load_verified_rules(request: Request, pin: RulePin | None) -> VerifiedRules:
    try:
        with fail_after(min(15, settings.execution_classification_seconds)):
            return await request.app.state.storage_worker.run(read_verified_rules, settings, pin)
    except TimeoutError:
        raise classification_model.ClassificationModelError("primary_specification_unavailable") from None


def pin_from_row(row) -> RulePin:
    supplementary = None
    if row["supplementary_sha256"] is not None:
        supplementary = RuleDocument("Invoice.Breakdown.Codex.md", row["supplementary_version"], row["supplementary_sha256"])
    return RulePin(primary_document(row["primary_sha256"], row["primary_version"]), supplementary)


class ConflictingRuleHistory(HTTPException):
    def __init__(self):
        super().__init__(409, "Earlier classifications used conflicting rules. Explicitly upgrade the task to an approved version.")


async def rule_pin(connection, revision, *, infer_legacy: bool = True) -> RulePin | None:
    task_id, number = revision["task_id"], revision["revision"]
    row = (await connection.execute(
        select(task_rule_pins).where(
            task_rule_pins.c.task_id == task_id, task_rule_pins.c.revision <= number,
        ).order_by(task_rule_pins.c.revision.desc()).limit(1),
    )).mappings().one_or_none()
    if row is not None:
        return pin_from_row(row)
    if not infer_legacy:
        return None

    # Keep earlier classifications on their documented version. Failed attempts
    # did not establish rules for a published proposal.
    legacy = (await connection.execute(select(
        source_classifications.c.skill_sha256,
        source_classifications.c.report["supplementary_rules"].label("supplementary"),
    ).where(
        source_classifications.c.task_id == task_id, source_classifications.c.revision <= number,
        source_classifications.c.status != "failed", source_classifications.c.skill_sha256.is_not(None),
    ).distinct())).mappings().all()
    if not legacy:
        return None
    pins = set()
    for evidence in legacy:
        supplementary = evidence["supplementary"] or {}
        document = None
        if supplementary.get("status") == "applied" and supplementary.get("sha256") and supplementary.get("version"):
            document = RuleDocument("Invoice.Breakdown.Codex.md", supplementary["version"], supplementary["sha256"])
        pins.add(RulePin(primary_document(evidence["skill_sha256"]), document))
    if len(pins) != 1:
        raise ConflictingRuleHistory()
    return pins.pop()


async def save_pin(connection, task_id: UUID, revision: int, pin: RulePin):
    await connection.execute(insert(task_rule_pins).values(
        task_id=task_id, revision=revision, primary_sha256=pin.primary.sha256, primary_version=pin.primary.version,
        supplementary_sha256=pin.supplementary.sha256 if pin.supplementary else None,
        supplementary_version=pin.supplementary.version if pin.supplementary else None,
    ))


class RuleUpgrade(RevisionRequest):
    primary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UpgradeReceipt(BaseModel):
    task: TaskDetails
    rules: RulePin


@router.get("")
async def read_rules(
    task_id: UUID, request: Request, operator: Operator,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> RulePin | None:
    async with transaction(database_for(request)) as connection:
        current = await owned_revision(connection, task_id, operator, revision=revision)
        return await rule_pin(connection, current)


@router.post("/upgrade")
async def upgrade_rules(
    task_id: UUID, payload: RuleUpgrade, request: Request, operator: Operator, idempotency_key: RequestKey = None,
) -> UpgradeReceipt:
    engine = database_for(request)
    key = ReceiptKey.from_request(operator, "rule_upgrade", idempotency_key, [task_id, payload])
    async with transaction(engine) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return UpgradeReceipt.model_validate(receipt)
        await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        if payload.primary_sha256 != settings.classification_skill_sha256:
            raise HTTPException(409, "Select the configured approved rules checksum")

    try:
        verified = await load_verified_rules(request, None)
    except classification_model.ClassificationModelError as error:
        raise HTTPException(503, {"code": error.code, "message": "Approved primary rules are unavailable"}) from None

    async with transaction(engine) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return UpgradeReceipt.model_validate(receipt)
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        if verified.pin.primary.sha256 != payload.primary_sha256 or payload.primary_sha256 != settings.classification_skill_sha256:
            raise HTTPException(409, "The configured approved rules have changed")
        try:
            previous = await rule_pin(connection, current)
        except ConflictingRuleHistory:
            previous = None
        if previous == verified.pin:
            raise HTTPException(409, "This task already uses the selected rules")
        updated = await append_revision(connection, current, {})
        await save_pin(connection, task_id, updated.revision, verified.pin)
        return await save_receipt(connection, key, UpgradeReceipt(task=updated, rules=verified.pin))
