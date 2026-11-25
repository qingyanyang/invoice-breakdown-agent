"""Validate classification proposals and carry source money into assignments."""

from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.classification_evidence import (
    COMBINED_FLOORS, GLAZING, GLAZING_EXCLUDED, ClassificationEvidence, ClassificationEvidenceError, stated_trade,
)
from app.reconcile import cents
from app.reconciliation_evidence import clean

CLASSIFIER_VERSION = "3"
ADDITIONAL = "Additional Items"


class Trade(StrEnum):
    PARTITION = "Partition"
    GLAZING = "Glazing"
    CEILING = "Ceiling"
    DOORS = "Doors"
    MAKE_GOOD = "Make Good"
    ACCESS_PANEL = "Access Panel"
    OTHER = "Other"


class RowProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    row: str = Field(min_length=1, max_length=255)
    trade: Trade | None
    trade_evidence: list[str] = Field(max_length=12)
    area: str | None = Field(max_length=255)
    glazing_price: str | None = Field(max_length=255)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    rows: list[RowProposal] = Field(max_length=300)

    @classmethod
    def from_storage(cls, proposal: dict):
        # Earlier proposals included model-authored free text. Rebuild the
        # rationale from validated source references when replaying them.
        return cls.model_validate({"rows": [
            {key: value for key, value in row.items() if key != "reason"} for row in proposal["rows"]
        ]})


def make_evidence(extraction: dict, report: dict) -> ClassificationEvidence:
    with localcontext() as context:
        context.prec = 256
        return ClassificationEvidence(extraction, report)


def failure_report(code: str) -> dict:
    return {"status": "failed", "error": code, "questions": [], "assignments": []}


def build_classification(
    evidence: ClassificationEvidence, proposal: Proposal, decisions: dict, identity: str,
) -> dict:
    sources = {row.row: row for row in proposal.rows}
    if len(sources) != len(proposal.rows) or sources.keys() != evidence.rows.keys():
        raise ClassificationEvidenceError("proposal_coverage_mismatch")

    for key, proposed in sources.items():
        text = evidence.row_context[key].text
        refs = proposed.trade_evidence
        label = evidence.rows[key].label.key
        if len(set(refs)) != len(refs) or not set(refs).issubset(text) or proposed.trade and label not in refs:
            raise ClassificationEvidenceError("invalid_trade_evidence")

    with localcontext() as context:
        context.prec = 256
        return ClassificationRun(evidence, decisions, identity).classify(sources)


class ClassificationRun:
    def __init__(self, evidence: ClassificationEvidence, decisions: dict, identity: str):
        self.evidence = evidence
        self.decisions = decisions
        self.identity = identity
        self.questions = []
        self.assignments = []

    def ask(self, kind, row, prompt, options, refs, *, requires_reason=False):
        key = f"{kind}:{row}"
        if decision := self.decisions.get(key):
            if decision["answer"] in options:
                return decision["answer"]
        self.questions.append({
            "id": str(uuid5(NAMESPACE_URL, f"{self.identity}:{key}")),
            "key": key, "kind": kind, "row": row, "prompt": prompt, "options": options,
            "evidence": list(dict.fromkeys(refs)), "requires_reason": requires_reason,
        })
        return None

    def classify(self, sources: dict[str, RowProposal]) -> dict:
        panel_notes = self.evidence.panel_exclusions()
        model_reviews, coverage = [], []
        for key, row in self.evidence.rows.items():
            proposed = sources[key]
            refs = list(dict.fromkeys([row.label.key, *proposed.trade_evidence]))
            scope_hold = False
            explicit = stated_trade(row.description)
            area, area_refs, area_origin = self.resolve_area(key, proposed.area)
            pieces, origin = self.resolve_trade(key, proposed, refs, explicit)
            if panel_notes and pieces and (
                explicit == Trade.ACCESS_PANEL or any(trade == Trade.ACCESS_PANEL for trade, _, _ in pieces)
            ):
                scope_hold = self.resolve_scope(row, panel_notes)
            model_reviews.append({
                "row": key, "trade": proposed.trade, "evidence": refs,
                "reason": (
                    f"The model proposed {proposed.trade} from {row.label.key}: {row.description}"
                    if proposed.trade else f"The model requested clarification on {row.label.key}: {row.description}"
                ),
                "area": proposed.area, "glazing_price": proposed.glazing_price,
            })
            assigned = bool(pieces) and area is not None and not scope_hold
            assigned_amount = sum((amount for _, amount, _ in pieces), Decimal(0)) if assigned else None
            coverage.append({
                "row": key, "working_amount": format(row.amount, "f"),
                "assigned_amount": format(assigned_amount, "f") if assigned else None,
                "assignment_count": len(pieces) if assigned else 0,
            })
            if not assigned:
                continue
            if assigned_amount != row.amount:
                raise ClassificationEvidenceError("classification_amount_mismatch")
            for trade, amount, arithmetic in pieces:
                self.assignments.append({
                    "row": key, "description": row.description, "trade": trade, "area": area,
                    "amount": format(amount, "f"), "trade_origin": origin, "trade_evidence": refs,
                    "area_origin": area_origin, "area_evidence": area_refs, "arithmetic": arithmetic,
                    "scope_origin": row.scope_origin, "markup_treatment": row.markup,
                    "markup_evidence": row.markup_evidence,
                    "protected_by_default": trade in (Trade.MAKE_GOOD, Trade.ACCESS_PANEL),
                    "rule_sections": ["6", "7", "8", "11"],
                })

        status = "blocked" if self.questions else "ready_for_allocation"
        total = None
        if status == "ready_for_allocation":
            total = sum((Decimal(item["amount"]) for item in self.assignments), Decimal(0))
            if any(row["assignment_count"] == 0 for row in coverage) or cents(total) != self.evidence.total:
                raise ClassificationEvidenceError("classification_amount_mismatch")

        return {
            "status": status, "error": None, "questions": self.questions, "assignments": self.assignments,
            "row_coverage": coverage, "model_proposals": model_reviews, "rationale_origin": "current_workbook",
            "gst_basis": self.evidence.gst_basis,
            "working_scope_total": format(self.evidence.total, "f"),
            "classified_total_exact": format(total, "f") if total is not None else None,
            "classified_total": format(cents(total), "f") if total is not None else None,
            "structural_checks": {
                "proposal_row_coverage": "passed",
                "assignment_amount_conservation": "passed" if total is not None else "pending",
            },
        }

    def resolve_area(self, key: str, proposed_area: str | None):
        candidates = self.evidence.row_context[key].areas
        if not candidates:
            return ADDITIONAL, [], "unallocated"
        if len(candidates) == 1:
            area, refs = next(iter(candidates.items()))
            if COMBINED_FLOORS.fullmatch(area):
                prompt = (
                    f"The source amount at {key} covers {area} without a separate amount for each floor. "
                    "Confirm keeping the combined attribution."
                )
            elif proposed_area is not None and clean(proposed_area) != clean(area):
                prompt = f"The proposed area differs from the source label at {key}. Confirm the source attribution."
            else:
                return area, refs, "workbook"
            area = self.ask("area", key, prompt, [area], refs)
        else:
            area = self.ask(
                "area", key, f"Which of the source area labels applies to {key}?",
                list(dict.fromkeys([*candidates, ADDITIONAL])),
                [ref for refs in candidates.values() for ref in refs],
            )
        return area, candidates.get(area, []), "operator_confirmed"

    def resolve_trade(self, key: str, proposed: RowProposal, refs: list[str], explicit: str | None):
        row = self.evidence.rows[key]
        prices = self.evidence.glazing_prices(row)
        candidates = {key: evidence for key, evidence in prices.items() if evidence["amount"] is not None}
        if proposed.glazing_price in candidates and proposed.trade in (None, Trade.PARTITION):
            return self.split(row, candidates[proposed.glazing_price]), "model_proposed"

        description = clean(row.description)
        combined = GLAZING.search(description) and "partition" in description.split()
        if combined or prices or proposed.glazing_price is not None:
            unsplit_trades = [Trade.PARTITION] if GLAZING_EXCLUDED.search(description) else list(Trade)
            choice = self.ask(
                "combined_trade", key,
                f"How should the combined trade amount at {key} be classified? "
                "A separate Glazing amount requires source evidence that the priced component is included "
                "in the parent on the same markup basis.",
                [*candidates, *[f"keep:{trade}" for trade in unsplit_trades]],
                [*refs, *[ref for price in prices.values() for ref in (price["label"], price["price"])]],
                requires_reason=True,
            )
            if choice is None:
                return [], None
            if choice in candidates:
                return self.split(row, candidates[choice]), "operator_confirmed"
            return [(Trade(choice.removeprefix("keep:")), row.amount, None)], "operator_confirmed"

        if proposed.trade is None or explicit and proposed.trade != explicit:
            trade = self.ask(
                "trade", key, f"Which trade does the description at {row.label.key} support?"
                + (f" The explicit workflow mapping suggests {explicit}." if explicit else ""),
                list(Trade), refs, requires_reason=True,
            )
            return ([(Trade(trade), row.amount, None)], "operator_confirmed") if trade else ([], None)
        return [(proposed.trade, row.amount, None)], "model_proposed"

    @staticmethod
    def split(row, price):
        glazing = price["amount"]
        remainder = row.amount - glazing
        evidence = {
            "parent": row.reference.key, "parent_amount": format(row.amount, "f"),
            "component": price["price"], "component_label": price["label"],
            "inclusion_reference": price["inclusion_reference"],
            "component_amount": format(glazing, "f"), "component_coefficient": row.coefficient,
            "remainder": format(remainder, "f"),
        }
        return [
            (Trade.GLAZING, glazing, {**evidence, "operation": "signed_component"}),
            (Trade.PARTITION, remainder, {**evidence, "operation": "parent_minus_component"}),
        ]

    def resolve_scope(self, row, notes):
        key = row.reference.key
        action = self.ask(
            "access_panel_scope", key,
            f"Access Panel exclusion notes conflict with priced detail at {key}. "
            "Confirm the current reconciled scope or request a scope change.",
            ["retain_scope", "change_scope"], [row.label.key, key, *[note["reference"] for note in notes]],
            requires_reason=True,
        )
        if action == "change_scope":
            self.questions.append({
                "id": str(uuid5(NAMESPACE_URL, f"{self.identity}:reconcile_scope:{key}")),
                "key": f"reconcile_scope:{key}", "kind": "reconcile_scope", "row": key,
                "prompt": f"Update the authorized scope in reconciliation before classifying {key}.",
                "options": [], "evidence": [row.label.key, key], "requires_reason": False,
            })
        return action != "retain_scope"
