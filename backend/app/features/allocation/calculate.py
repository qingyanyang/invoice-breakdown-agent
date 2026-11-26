"""Deterministic target allocation from an approved classification and scope bridge."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from app.features.classification.calculate import Trade
from app.money import CENT, amount_text, cents
from app.features.extraction.references import Reference
from app.features.extraction.references import SourceKey

ALLOCATION_VERSION = "2"
PRECISION = 512
ALLOCATION_CONTEXT = Context(prec=PRECISION, rounding=ROUND_HALF_UP)
ZERO = Decimal(0)
ONE = Decimal(1)
HALF_CENT = Decimal("0.005")
DEFAULT_PROTECTED = {Trade.MAKE_GOOD, Trade.ACCESS_PANEL}


class DisplayCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: SourceKey
    trade: Trade

    @property
    def key(self) -> tuple[str, Trade]:
        return self.area, self.trade


class AllocationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected_rows: list[SourceKey] = Field(default_factory=list, max_length=300)
    rounding_preference: DisplayCell | None = None
    requested_direction: Literal["increase", "decrease"] | None = None

    @field_validator("protected_rows")
    @classmethod
    def distinct_rows(cls, rows: list[str]) -> list[str]:
        if len(set(rows)) != len(rows):
            raise ValueError("Select each protected row at most once")
        return sorted(rows)


class Assignment(BaseModel):
    row: SourceKey
    area: SourceKey
    trade: Trade
    amount: Decimal = Field(allow_inf_nan=False)
    scope_origin: Literal["in_source", "authorized_addition"]
    protected_by_default: bool


assignments_adapter = TypeAdapter(list[Assignment])


class AllocationProblem(ValueError):
    def __init__(self, code: str, detail: str, evidence: dict | None = None):
        self.code = code
        self.detail = detail
        self.evidence = evidence


def verify_working_scope(reconciliation: dict, classification: dict) -> tuple[list[Assignment], Decimal]:
    def key(item):
        return Reference.from_dict(item["reference"]).key

    exclusions = {key(item) for item in reconciliation["scope_exclusions"]}
    expected = {
        (key(item), "in_source"): Decimal(item["contribution"])
        for item in reconciliation["selected_rows"] if key(item) not in exclusions
    }
    expected.update({
        (key(item), "authorized_addition"): Decimal(item["effect"])
        for item in reconciliation["scope_additions"]
    })
    try:
        assignments = assignments_adapter.validate_python(classification["assignments"])
    except ValidationError:
        raise AllocationProblem("invalid_classification", "The current classification has invalid assignments.") from None

    assigned = defaultdict(Decimal)
    for assignment in assignments:
        assigned[assignment.row, assignment.scope_origin] += assignment.amount
        if assignment.protected_by_default != (assignment.trade in DEFAULT_PROTECTED):
            raise AllocationProblem("invalid_classification", "An assignment has inconsistent default protection.")
    working = sum(expected.values(), ZERO)
    if (
        assigned != expected or working != Decimal(classification["classified_total_exact"])
        or cents(working) != Decimal(reconciliation["working_scope_total"])
        or cents(working) != Decimal(classification["working_scope_total"])
        or classification["gst_basis"] != reconciliation["gst_basis"]
    ):
        raise AllocationProblem("working_scope_mismatch", "Assignments do not reconcile to the approved working scope.")
    return assignments, working


@dataclass
class Components:
    protected: Decimal = ZERO
    adjustable: Decimal = ZERO


@dataclass
class RoundedCell:
    source: Components
    protected: Decimal
    adjustable: Decimal
    residual: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.protected + self.adjustable + self.residual


@dataclass(frozen=True)
class Scale:
    numerator: Decimal
    denominator: Decimal

    def amount(self, source: Decimal) -> Decimal:
        return (source * self.numerator) / self.denominator

    def rounded(self, source: Decimal) -> Decimal:
        # Round the exact quotient to cents, even if the factor is recurring.
        scaled_cents = (source * self.numerator).scaleb(2)
        whole_cents, remainder = divmod(scaled_cents.copy_abs(), self.denominator)
        if remainder * 2 >= self.denominator:
            whole_cents += 1
        return whole_cents.scaleb(-2).copy_sign(scaled_cents) if whole_cents else ZERO.quantize(CENT)


IDENTITY = Scale(ONE, ONE)


def choose_scale(target: Decimal, protected: Decimal, base: Decimal) -> Scale | None:
    if target < protected:
        raise AllocationProblem("target_below_protected", "The target cannot cover the approved protected amounts.")
    if base == ZERO:
        if target == protected:
            return None
        raise AllocationProblem("unattainable_target", "The adjustable base is zero and the target differs from the protected total.")
    if base < ZERO:
        raise AllocationProblem("negative_adjustable_base", "The signed adjustable base would require an invalid factor. Review the approved scope.")
    return Scale(target - protected, base)


def classify_protection(assignment: Assignment, protected_rows: set[str]) -> str | None:
    if assignment.protected_by_default:
        return "default_trade"
    return "owner_designated" if assignment.row in protected_rows else None


def check_amplification(scale: Scale, lines: list[tuple[Assignment, str | None]]) -> None:
    uplift = scale.numerator - scale.denominator
    if uplift <= ZERO:
        return
    credits = [item for item, protection in lines if protection is None and item.amount < ZERO]
    credit_magnitude = sum((item.amount.copy_abs() for item in credits), ZERO)
    # Compare the additional offsetting exposure with the full adjustable target.
    # Cross-multiplication makes the gate independent of a rounded quotient.
    if credit_magnitude * uplift > scale.numerator * scale.denominator:
        projected = scale.amount(credit_magnitude)
        raise AllocationProblem(
            "implausible_factor",
            "The factor increases offsetting credits by more than the authorized adjustable target. "
            "Review the contributing scope and target before allocation.",
            {
                "adjustable_base": amount_text(scale.denominator),
                "adjustable_target": amount_text(scale.numerator),
                "proposed_factor": amount_text(scale.amount(ONE)),
                "source_credit_magnitude": amount_text(credit_magnitude),
                "projected_credit_magnitude": amount_text(projected),
                "credit_increase": amount_text(projected - credit_magnitude),
                "credit_rows": [item.row for item in credits],
            },
        )


def residual_capacity(cell: RoundedCell, trade_total: Decimal, delta: Decimal) -> Decimal:
    magnitude = delta.copy_abs()
    if cell.source.adjustable.is_signed() != delta.is_signed():
        magnitude = min(magnitude, (cell.adjustable + cell.residual).copy_abs())
    if delta < ZERO:
        magnitude = min(magnitude, max(trade_total, ZERO))
    return magnitude.copy_sign(delta)


def apply_rounding_residual(cells, delta, displayed_trade, policy, scale):
    if not delta:
        return []

    eligible = {
        key for key, cell in cells.items() if scale.numerator > ZERO and cell.source.adjustable != ZERO
    }
    preferred = policy.rounding_preference.key if policy.rounding_preference else None
    if preferred is not None and preferred not in eligible:
        raise AllocationProblem("ineligible_rounding_preference", "Select a displayed cell with an adjustable amount.")
    # The factor is uniform, so source magnitudes give the same ranking as scaled magnitudes.
    ordered = [preferred] if preferred is not None else sorted(
        eligible, key=lambda key: (-cells[key].source.adjustable.copy_abs(), key),
    )
    negative_trades = {trade for trade, amount in displayed_trade.items() if amount < ZERO}
    remaining = delta
    recipients = {}

    def apply(key, amount):
        nonlocal remaining
        if amount:
            cells[key].residual += amount
            displayed_trade[key[1]] += amount
            remaining -= amount
            recipients[key] = None

    for key in ordered:
        trade_total = displayed_trade[key[1]]
        if (
            negative_trades.issubset({key[1]}) and trade_total + delta >= ZERO
            and residual_capacity(cells[key], trade_total, delta) == delta
        ):
            apply(key, delta)
            return list(recipients)

    problem = AllocationProblem(
        "unsafe_rounding_residual",
        "Eligible components cannot absorb the residual within their approved signs and trade totals.",
    )
    minimum = -sum((displayed_trade[trade] for trade in negative_trades), ZERO)
    if preferred is not None or (negative_trades and minimum > delta):
        raise problem

    # Reserve any positive residual needed to restore a rounded trade total to zero.
    for key in ordered:
        trade_total = displayed_trade[key[1]]
        if trade_total < ZERO:
            apply(key, residual_capacity(cells[key], trade_total, -trade_total))
    if any(displayed_trade[trade] < ZERO for trade in negative_trades):
        raise problem

    for key in ordered:
        if not remaining:
            break
        apply(key, residual_capacity(cells[key], displayed_trade[key[1]], remaining))
    if remaining:
        raise problem
    return list(recipients)


def amount_map(totals: dict) -> dict:
    return {key: amount_text(value) for key, value in sorted(totals.items())}


def build_display(buckets, scale, expected, policy, *, has_target: bool) -> dict:
    subtotal_numerator = ZERO
    exact_trade = defaultdict(Decimal)
    displayed_trade = defaultdict(Decimal)
    cells = {}
    rounded_count = 0
    for key, components in sorted(buckets.items()):
        exact = components.protected * scale.denominator + components.adjustable * scale.numerator
        exact_trade[key[1]] += exact
        subtotal_numerator += exact
        cell = RoundedCell(components, cents(components.protected), scale.rounded(components.adjustable))
        cells[key] = cell
        rounded_count += bool(components.protected) + bool(components.adjustable and scale.numerator)
        displayed_trade[key[1]] += cell.total

    expected_display = cents(expected)
    if subtotal_numerator != expected * scale.denominator:
        raise AllocationProblem("allocation_total_mismatch", "The unrounded allocations do not sum to the approved total.")
    if has_target and any(value < ZERO for value in exact_trade.values()):
        raise AllocationProblem("negative_trade_allocation", "The approved inputs would produce a negative trade total.")

    before = sum(displayed_trade.values(), ZERO)
    residual = expected_display - before
    # Source-only runs compare against the rounded source total, which can itself differ by half a cent.
    bound = HALF_CENT * (rounded_count + (not has_target and expected != expected_display))
    if residual.copy_abs() > bound:
        raise AllocationProblem("unexplained_rounding_difference", "The displayed difference exceeds the maximum rounding error.")
    recipients = apply_rounding_residual(cells, residual, displayed_trade, policy, scale) if has_target else []
    applied = sum((cells[key].residual for key in recipients), ZERO)

    displayed_area = defaultdict(Decimal)
    displayed_trade.clear()
    for (area, trade), cell in cells.items():
        displayed_area[area] += cell.total
        displayed_trade[trade] += cell.total
    total = cents(sum(displayed_area.values(), ZERO))
    difference = cents(total - expected_display)
    if has_target and (difference or any(value < ZERO for value in displayed_trade.values())):
        raise AllocationProblem("display_total_mismatch", "Displayed trade totals must be nonnegative and reconcile to the authorized target.")

    return {
        "cells": [
            {
                "area": area, "trade": trade,
                "protected_exact": amount_text(cell.source.protected),
                "protected_display": amount_text(cell.protected),
                "adjustable_base": amount_text(cell.source.adjustable),
                "adjustable_display_before_residual": amount_text(cell.adjustable),
                "adjustable_display": amount_text(cents(cell.adjustable + cell.residual)),
                "rounding_residual": amount_text(cents(cell.residual)),
                "display_total": amount_text(cents(cell.total)),
            }
            for (area, trade), cell in cells.items()
        ],
        "area_totals": amount_map(displayed_area),
        "trade_totals": amount_map(displayed_trade),
        "total": amount_text(total),
        "rounding": {
            "purpose": "target_reconciliation" if has_target else "source_presentation",
            "mode": ROUND_HALF_UP, "decimal_precision": PRECISION,
            "rounded_component_count": rounded_count, "maximum_difference": amount_text(bound),
            "comparison_total": amount_text(expected_display), "before_residual": amount_text(cents(before)),
            "residual_required": amount_text(cents(residual)),
            "residual_applied": amount_text(cents(applied)),
            "recipient": {"area": recipients[0][0], "trade": recipients[0][1]} if len(recipients) == 1 else None,
            "recipients": [
                {"area": area, "trade": trade, "amount": amount_text(cents(cells[area, trade].residual))}
                for area, trade in recipients
            ],
            "note": "rounding residual applied" if recipients else None,
            "difference": amount_text(difference),
        },
    }


def allocate(
    reconciliation: dict, classification: dict, target: Decimal | None, target_basis: str | None,
    policy: AllocationPolicy,
) -> dict:
    has_target = target is not None
    report = {
        "mode": "target_allocation" if has_target else "no_contract_price_adjustment",
        "target": amount_text(target), "target_gst_basis": target_basis,
        "source_gst_basis": reconciliation["gst_basis"],
        "rule_sections": ["SKILL.md §8", "SKILL.md §9", "SKILL.md §11"],
    }
    try:
        with localcontext(ALLOCATION_CONTEXT):
            if has_target and target_basis not in {"excluding_gst", "including_gst"}:
                raise AllocationProblem("target_gst_unconfirmed", "Confirm the target GST basis in the current workflow.")
            if has_target and (target_basis != "excluding_gst" or reconciliation["gst_basis"] != "excluding_gst"):
                raise AllocationProblem("gst_conversion_required", "Allocation requires a verified ex-GST source and target or an approved conversion basis.")
            assignments, working = verify_working_scope(reconciliation, classification)
            return {**report, **allocate_working_scope(assignments, working, target, policy)}
    except AllocationProblem as error:
        issue = {"code": error.code, "detail": error.detail}
        if error.evidence is not None:
            issue["evidence"] = error.evidence
        return {**report, "status": "blocked", "factor": None, "issue": issue}


def allocate_working_scope(assignments, working, target, policy):
    protected_rows = set(policy.protected_rows)
    if not protected_rows.issubset(assignment.row for assignment in assignments):
        raise AllocationProblem("invalid_protected_row", "Each extra protected row must be in the approved working scope.")
    buckets = defaultdict(Components)
    protected_by_origin = defaultdict(Decimal)
    lines = []
    for assignment in sorted(assignments, key=lambda item: (item.area, item.trade, item.row)):
        protection = classify_protection(assignment, protected_rows)
        lines.append((assignment, protection))
        cell = buckets[assignment.area, assignment.trade]
        if protection:
            cell.protected += assignment.amount
            protected_by_origin[assignment.scope_origin] += assignment.amount
        else:
            cell.adjustable += assignment.amount

    protected = sum(protected_by_origin.values(), ZERO)
    base = working - protected
    scale = choose_scale(target, protected, base) if target is not None else None
    if scale is not None:
        check_amplification(scale, lines)
    effective_scale = scale or IDENTITY
    direction = None
    if target is not None:
        direction = "increase" if target > working else "decrease" if target < working else "unchanged"
    mismatch = (
        policy.requested_direction is not None and direction in {"increase", "decrease"}
        and policy.requested_direction != direction
    )

    display = build_display(
        buckets, effective_scale, target if target is not None else working, policy, has_target=target is not None,
    )
    return {
        "status": "ready_for_workbook", "issue": None,
        "working_total": amount_text(working), "protected_total": amount_text(protected),
        "protected_in_source": amount_text(protected_by_origin["in_source"]),
        "protected_authorized_additions": amount_text(protected_by_origin["authorized_addition"]),
        "adjustable_base": amount_text(base),
        "adjustable_target": amount_text(target - protected) if target is not None else None,
        "factor": {
            "decimal": amount_text(scale.amount(ONE)), "numerator": amount_text(scale.numerator),
            "denominator": amount_text(scale.denominator),
        } if scale is not None else None,
        "actual_direction": direction,
        "warnings": ["direction_mismatch"] if mismatch else [],
        "assignments": [
            {
                "row": item.row, "area": item.area, "trade": item.trade, "scope_origin": item.scope_origin,
                "protection": protection, "source_amount": amount_text(item.amount),
                "unrounded_amount": amount_text(item.amount if protection else effective_scale.amount(item.amount)),
            }
            for item, protection in lines
        ],
        "display": display,
    }
