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
PINNED_SUPPLEMENT_ERRORS = {
    "pinned_supplementary_unavailable", "pinned_supplementary_mismatch", "pinned_supplementary_contains_evaluation_data",
}
EVALUATION_DATA_ERRORS = {"primary_specification_contains_evaluation_data", "pinned_supplementary_contains_evaluation_data"}
RULE_INPUT_ERRORS = PINNED_SUPPLEMENT_ERRORS | EVALUATION_DATA_ERRORS | {
    "primary_specification_mismatch", "primary_specification_unavailable",
}


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


def archived_rule(config: Settings, document: RuleDocument) -> Path | None:
    if config.classification_rules_archive and re.fullmatch(r"[0-9a-f]{64}", document.sha256):
        return config.classification_rules_archive / document.sha256 / document.document
    return None


def read_pinned_supplement(config: Settings, document: RuleDocument) -> classification_model.SupplementarySpecification:
    rejected = set()
    for path in (config.classification_codex_path, archived_rule(config, document)):
        supplement = classification_model.read_supplementary_specification(
            path, version=document.version, sha256=document.sha256,
        )
        if supplement.status == "applied":
            return supplement
        rejected.add(supplement.status)
    if "contains_evaluation_data" in rejected:
        code = "pinned_supplementary_contains_evaluation_data"
    elif "checksum_mismatch" in rejected:
        code = "pinned_supplementary_mismatch"
    else:
        code = "pinned_supplementary_unavailable"
    raise classification_model.ClassificationModelError(code)


def read_verified_rules(config: Settings, pin: RulePin | None) -> VerifiedRules:
    expected = pin.primary if pin else primary_document(config.classification_skill_sha256, config.classification_skill_version)
    rejected = set()
    for path in (config.classification_skill_path, archived_rule(config, expected) if pin else None):
        if path is None:
            continue
        try:
            specification = classification_model.read_primary_specification(path, expected_sha256=expected.sha256)
        except classification_model.ClassificationModelError as error:
            rejected.add(error.code)
            continue

        if pin is None:
            supplement = classification_model.read_supplementary_specification(config.classification_codex_path)
        elif pin.supplementary:
            supplement = read_pinned_supplement(config, pin.supplementary)
        else:
            status = "version_not_pinned" if config.classification_codex_path else "not_configured"
            supplement = classification_model.SupplementarySpecification(status)
        specification = replace(specification, supplementary=supplement)
        return VerifiedRules(pin or RulePin(expected, applied_supplementary(specification)), specification)

    code = next((error for error in (
        "primary_specification_contains_evaluation_data", "primary_specification_mismatch",
    ) if error in rejected), "primary_specification_unavailable")
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
    supplementary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


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
    # Omission preserves the rule set. An explicit null opts into primary-only use.
    key = ReceiptKey.from_request(operator, "rule_upgrade", idempotency_key, [task_id, payload.model_dump(exclude_unset=True)])
    declares_supplement = "supplementary_sha256" in payload.model_fields_set
    async with transaction(engine) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return UpgradeReceipt.model_validate(receipt)
        await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        if payload.primary_sha256 != settings.classification_skill_sha256:
            raise HTTPException(409, "Select the configured approved rules checksum")

    try:
        verified = await load_verified_rules(request, None)
    except classification_model.ClassificationModelError as error:
        message = "Move evaluation data out of the primary rules" if error.code in EVALUATION_DATA_ERRORS else (
            "Approved primary rules are unavailable"
        )
        raise HTTPException(503, {"code": error.code, "message": message}) from None

    selected = verified.pin
    if declares_supplement:
        if payload.supplementary_sha256 is None:
            selected = RulePin(selected.primary)
        elif not selected.supplementary or payload.supplementary_sha256 != selected.supplementary.sha256:
            raise HTTPException(409, "Select the configured and verified supplementary rules checksum")

    async with transaction(engine) as connection:
        if (receipt := await reserve_owned_receipt(connection, task_id, key)) is not None:
            return UpgradeReceipt.model_validate(receipt)
        current = await lock_current_revision(connection, task_id, operator, payload.expected_revision)
        if verified.pin.primary.sha256 != payload.primary_sha256 or payload.primary_sha256 != settings.classification_skill_sha256:
            raise HTTPException(409, "The configured approved rules have changed")
        try:
            previous = await rule_pin(connection, current)
        except ConflictingRuleHistory:
            if not declares_supplement:
                raise HTTPException(409, "Explicitly select the supplementary rules checksum or null") from None
            previous = None
        if previous and not declares_supplement:
            selected = RulePin(selected.primary, previous.supplementary)
        if previous == selected:
            raise HTTPException(409, "This task already uses the selected rules")
        updated = await append_revision(connection, current, {})
        await save_pin(connection, task_id, updated.revision, selected)
        return await save_receipt(connection, key, UpgradeReceipt(task=updated, rules=selected))
