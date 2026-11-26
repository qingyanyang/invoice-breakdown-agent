"""Build a source-backed control total and authorized scope bridge."""

from decimal import Decimal, localcontext
from math import prod

from app.money import cents, amount_text
from app.features.reconciliation.evidence import (
    MAX_REFERENCES,
    SourceRow,
    WorkbookEvidence,
    additive_references,
    explicitly_outside_total,
    markup_recipe,
    numeric,
    quantity_rate_recipe,
    stated_tax_bases,
    tax_basis,
    total_kind,
)
from app.features.extraction.references import Reference

QUESTION_PROMPTS = {
    "source_control": "Which source cell is the final project control total?",
    "source_gst_basis": "What GST basis applies to this source control total?",
    "source_inclusion": "Does the reported source control include this signed scope row?",
    "markup_treatment": "Does this recorded source amount already include the listed markup?",
    "scope_addition_gst_basis": "What GST basis applies to this outside-scope addition?",
}
ISSUE_DETAILS = {
    "missing_source_control": "A final source Total could not be identified.",
    "missing_reported_total": "The reported Total is unavailable. Supply a source with the Total saved.",
    "unresolved_hierarchy": "The source hierarchy does not identify a non-overlapping amount set.",
    "non_overlapping_hierarchy_unresolved": "The dependency trace overlaps, cycles, or exceeds supported limits.",
    "unlabelled_contributing_amount": "The Total references an amount without a recognizable description or amount heading.",
    "unsupported_calculation_evidence": "The amount lacks independent supporting evidence. Supply the underlying inputs or a reconciled source export.",
    "missing_literal_amount": "An included detail row lacks a recorded numeric amount.",
    "cached_amount_discrepancy": "A saved amount differs from the amount supported by its source inputs.",
    "markup_amount_discrepancy": "The recorded amount matches neither the evidenced base nor the marked-up amount.",
    "untraced_source_rows": "These amount rows have no established contribution or exclusion. Confirm the source hierarchy with supporting evidence.",
    "control_total_discrepancy": "The independent total differs from the reported Total at cent precision. Supply corrected source evidence.",
    "missing_independent_total": "An independent control amount could not be established.",
    "mixed_contributing_gst_bases": "A contributing amount explicitly uses a different GST basis from the source control.",
    "scope_addition_gst_basis_mismatch": "This addition and the source control use different GST bases.",
}


class Reconciliation:
    def __init__(self, workbook: WorkbookEvidence, answers: dict):
        self.workbook = workbook
        self.answers = answers
        self.questions = []
        self.issues = []
        self.selected = {}
        self.groups = []
        self.visited = set()

    def ask(self, kind: str, ref: Reference | None, options: list[str], evidence: list[dict]) -> str | None:
        key = f"{kind}:{ref.key}" if ref is not None else kind
        answer = self.answers.get(key)
        if answer in options:
            return answer
        self.questions.append({
            "key": key, "kind": kind, "prompt": QUESTION_PROMPTS[kind],
            "options": options, "evidence": evidence,
        })
        return None

    def issue(self, code: str, refs) -> None:
        self.issues.append({"code": code, "detail": ISSUE_DETAILS[code], "evidence": [ref.evidence() for ref in refs]})

    def select_control(self):
        possible = [candidate for candidate in self.workbook.candidates if candidate.kind != "alternative"]
        project = [candidate for candidate in possible if candidate.kind == "project"]
        active = project or possible
        if any(tax_basis(candidate.row.description) == "excluding_gst" for candidate in active):
            active = [candidate for candidate in active if tax_basis(candidate.row.description) != "including_gst"]
        if len(active) == 1:
            return active[0]
        if not active:
            self.issue("missing_source_control", [])
            return None
        choice = self.ask(
            "source_control", None, [candidate.row.amount.key for candidate in active],
            [candidate.evidence() for candidate in active],
        )
        return next((candidate for candidate in active if candidate.row.amount.key == choice), None)

    def source_basis(self, row: SourceRow) -> tuple[str | None, str | None]:
        sources = dict(self.workbook.basis_evidence)
        stated = stated_tax_bases(row.description)
        meanings = set(sources.values()) | stated
        if len(meanings) == 1:
            return meanings.pop(), "workbook"
        answer = self.ask(
            "source_gst_basis", row.amount, ["excluding_gst", "including_gst"],
            [
                *[{"reference": ref.evidence(), "gst_basis": basis} for ref, basis in sources.items()],
                *[{"reference": row.label.evidence(), "gst_basis": basis} for basis in sorted(stated)],
            ] or [row.amount.evidence()],
        )
        return answer, "operator_confirmed" if answer else None

    def literal(self, ref: Reference | None) -> Decimal | None:
        return numeric(self.workbook.cells.get(ref), literal=True)

    def check_reported(self, ref: Reference, independent: Decimal) -> None:
        recorded = numeric(self.workbook.cells.get(ref))
        if recorded is not None and cents(recorded) != cents(independent):
            self.issue("cached_amount_discrepancy", [ref])

    def verify_markup_amount(self, row: SourceRow, formula: str) -> Decimal | None:
        recipe = markup_recipe(formula, row.amount.sheet)
        fields = row.fields
        if recipe is None:
            return None
        base, markup = recipe
        expected_base = {fields.get("base")} if len(base) == 1 else {fields.get("quantity"), fields.get("rate")}
        if set(base) != expected_base or markup != fields.get("markup"):
            return None
        numbers = [self.literal(ref) for ref in [*base, markup]]
        if any(value is None for value in numbers):
            return None
        amount = prod(numbers[:-1], start=Decimal(1)) * (1 + numbers[-1])
        self.check_reported(row.amount, amount)
        return amount

    def literal_markup(self, row: SourceRow, amount: Decimal) -> tuple[str | None, list[Reference]]:
        fields = row.fields
        markup_ref = fields.get("markup")
        if markup_ref is None:
            return "not_stated", []
        markup = self.literal(markup_ref)
        if markup == 0:
            return "zero", [markup_ref]

        bases = [fields["base"]] if "base" in fields else [
            fields[field] for field in ("quantity", "rate") if field in fields
        ]
        if len(bases) in {1, 2} and ("base" in fields or len(bases) == 2):
            numbers = [self.literal(ref) for ref in bases]
            fmt = self.workbook.cells[markup_ref].get("number_format", {}).get("code") or ""
            if all(value is not None for value in numbers):
                base = prod(numbers, start=Decimal(1))
                matches_base = cents(amount) == cents(base)
                if markup is not None and "%" in fmt:
                    matches_marked = cents(amount) == cents(base * (1 + markup))
                    if matches_base != matches_marked:
                        return ("excluded" if matches_base else "included"), [*bases, markup_ref]
                    if not matches_base:
                        self.issue("markup_amount_discrepancy", [row.amount, *bases, markup_ref])
                        return None, [*bases, markup_ref]

        answer = self.ask(
            "markup_treatment", row.amount, ["included", "excluded"],
            [row.amount.evidence(), markup_ref.evidence()],
        )
        return answer, [markup_ref]

    def leaf(self, row: SourceRow) -> tuple[Decimal | None, str | None, list[Reference]]:
        cell = self.workbook.cells.get(row.amount)
        formula = cell.get("formula") if cell else None
        if formula:
            expression = formula.get("text") or ""
            if (verified := self.verify_markup_amount(row, expression)) is not None:
                return verified, "included", [row.fields[field] for field in ("base", "quantity", "rate", "markup")
                                               if field in row.fields]
            quantity, rate = row.fields.get("quantity"), row.fields.get("rate")
            if quantity is not None and rate is not None and quantity_rate_recipe(
                expression, row.amount.sheet,
            ) == {quantity, rate}:
                qty_value, rate_value = self.literal(quantity), self.literal(rate)
                if qty_value is not None and rate_value is not None:
                    amount = qty_value * rate_value
                    self.check_reported(row.amount, amount)
                    return amount, "excluded" if "markup" in row.fields else "not_stated", [quantity, rate]
            self.issue("unsupported_calculation_evidence", [row.amount])
            return None, None, []
        amount = self.literal(row.amount)
        if amount is None:
            self.issue("missing_literal_amount", [row.amount])
            return None, None, []
        markup, evidence = self.literal_markup(row, amount)
        return amount, markup, evidence

    def select_leaf(
        self, row: SourceRow, sign: int, parent: Reference | None, reason: str,
        inherited_scope: str | None = None,
    ) -> Decimal | None:
        amount, markup, inputs = self.leaf(row)
        contribution = None if amount is None else sign * amount
        self.selected[row.amount] = {
            "reference": row.amount.evidence(), "description": row.description,
            "description_reference": row.label.evidence() if row.label else None,
            "parent": parent.evidence() if parent else None,
            "reason": reason, "scope_kind": row.scope_kind or inherited_scope, "source_amount": amount_text(amount),
            "coefficient": sign, "contribution": amount_text(contribution),
            "markup_treatment": markup, "markup_evidence": [ref.evidence() for ref in inputs],
        }
        return contribution

    def trace(
        self, ref: Reference, sign: int = 1, parent: Reference | None = None,
        depth: int = 0, inherited_scope: str | None = None,
    ) -> Decimal | None:
        if ref in self.visited or depth > 64 or len(self.visited) >= MAX_REFERENCES:
            self.issue("non_overlapping_hierarchy_unresolved", [ref])
            return None
        self.visited.add(ref)
        cell = self.workbook.cells.get(ref) or {}
        row = self.workbook.row_for(ref)
        formula = cell.get("formula")
        expression = formula.get("text") if formula else None
        children = additive_references(expression, ref.sheet) if expression else None
        if children is not None:
            self.groups.append({
                "reference": ref.evidence(),
                "children": [{"reference": child.evidence(), "coefficient": child_sign} for child, child_sign in children],
            })
            scope = row.scope_kind or inherited_scope
            values = [
                self.trace(child, sign * child_sign, ref, depth + 1, scope)
                for child, child_sign in children
            ]
            if any(value is None for value in values):
                return None
            total = sum(values, Decimal(0))
            if parent is not None:
                self.check_reported(ref, sign * total)
            return total
        if parent is None or total_kind(row.description, row.amount.sheet) in {"subtotal", "total", "project"}:
            self.issue("unsupported_calculation_evidence" if formula else "unresolved_hierarchy", [ref])
            return None
        if row.label is None:
            self.issue("unlabelled_contributing_amount", [ref])
            return None
        return self.select_leaf(row, sign, parent, "source_formula", inherited_scope)

    def trace_literal_total(
        self, control: SourceRow, source_rows: list[SourceRow],
    ) -> tuple[Decimal | None, dict[Reference, str]]:
        tables = [table for table in self.workbook.tables if any(row.amount == control.amount for row in table)]
        if len(tables) != 1:
            self.issue("unresolved_hierarchy", [control.amount])
            return None, {}

        omitted = {}
        values = []
        for row in tables[0]:
            if row.amount == control.amount:
                break
            if row.scope_kind:
                continue
            if total_kind(row.description, row.amount.sheet) in {"subtotal", "total", "project"}:
                self.issue("unresolved_hierarchy", [control.amount, row.amount])
                values.append(None)
                continue
            values.append(self.select_leaf(row, 1, control.amount, "flat_table_before_control"))

        # The row order alone does not establish the contribution of signed scope candidates.
        for row in source_rows:
            if row.scope_kind is None:
                continue
            if explicitly_outside_total(row.description):
                omitted[row.amount] = "explicit_source_total_exclusion"
                continue
            inclusion = self.ask(
                "source_inclusion", row.amount, ["included", "excluded"],
                [row.amount.evidence(), control.amount.evidence()],
            )
            if inclusion == "excluded":
                omitted[row.amount] = "operator_confirmed_source_exclusion"
            elif inclusion is None:
                values.append(None)
            elif total_kind(row.description, row.amount.sheet) is not None:
                self.issue("unresolved_hierarchy", [control.amount, row.amount])
                values.append(None)
            else:
                values.append(self.select_leaf(row, 1, control.amount, "operator_confirmed_source_inclusion"))
        total = sum(values, Decimal(0)) if values and all(value is not None for value in values) else None
        return total, omitted

    def check_coverage(self, source_rows: list[SourceRow], declared: bool, omitted: dict[Reference, str]) -> list[dict]:
        outside = []
        untraced = []
        traced_groups = {Reference.from_dict(group["reference"]) for group in self.groups}
        for row in source_rows:
            if row.amount in self.selected or row.amount in traced_groups:
                continue
            if row.scope_kind and (declared or row.amount in omitted):
                outside.append({
                    "reference": row.amount.evidence(), "description": row.description,
                    "scope_kind": row.scope_kind,
                    "reason": "not_referenced_by_source_total" if declared else omitted[row.amount],
                })
            elif not row.scope_kind:
                untraced.append(row.amount)
        if untraced:
            self.issue("untraced_source_rows", untraced)
        return outside

    def check_contributing_basis(self, control: SourceRow, basis: str | None) -> None:
        if basis is None:
            return
        refs = set(self.selected)
        refs.update(Reference.from_dict(group["reference"]) for group in self.groups)
        refs.discard(control.amount)
        conflicts = [
            ref for ref in sorted(refs)
            if (stated := tax_basis(self.workbook.row_for(ref).description)) and stated != basis
        ]
        if conflicts:
            self.issue("mixed_contributing_gst_bases", conflicts)

    def addition_basis(
        self, row: SourceRow, control: SourceRow, source_basis: str | None,
    ) -> tuple[str | None, str | None]:
        if source_basis is None:
            return None, None

        stated = stated_tax_bases(row.description)
        basis = next(iter(stated)) if len(stated) == 1 else None
        origin = "workbook"
        if basis is None:
            same_sheet = {
                ref: value for ref, value in self.workbook.basis_evidence.items()
                if ref.sheet == row.amount.sheet
            }
            meanings = set(same_sheet.values())
            if not stated and len(meanings) == 1:
                basis = meanings.pop()
            else:
                basis = self.ask(
                    "scope_addition_gst_basis", row.amount, ["excluding_gst", "including_gst"],
                    [row.amount.evidence(), control.amount.evidence(), *[
                        {"reference": ref.evidence(), "gst_basis": value}
                        for ref, value in same_sheet.items()
                    ], *[
                        {"reference": row.label.evidence(), "gst_basis": value}
                        for value in sorted(stated)
                    ]],
                )
                origin = "operator_confirmed"
        if basis is None:
            return None, None
        if basis != source_basis:
            self.issue("scope_addition_gst_basis_mismatch", [row.amount, control.amount])
            return None, None
        return basis, origin

    def require_control_correction(self, control: SourceRow, reported: Decimal, independent: Decimal) -> None:
        self.issue("control_total_discrepancy", [control.amount])
        self.questions.append({
            "key": f"source_total_correction:{control.amount.key}",
            "kind": "source_total_correction",
            "prompt": (
                f"Can you supply corrected source evidence for {control.amount.key}? "
                f"The workbook reports {cents(reported):f}, and the traced rows total {cents(independent):f}."
            ),
            "required_evidence": "Upload a corrected workbook with the final Total and the detail and subtotal evidence that supports it.",
            "response_type": "source_correction",
            "options": [],
            "evidence": [control.amount.evidence()],
        })

    def bridge(
        self, scope: dict, outside: list[dict], control: SourceRow, source_basis: str | None,
    ) -> tuple[list[dict], list[dict], Decimal]:
        selected = {ref.key: (ref, item) for ref, item in self.selected.items()}
        outside_rows = {Reference.from_dict(item["reference"]).key: item for item in outside}
        exclusions, additions = [], []
        delta = Decimal(0)

        for key in scope["exclude"]:
            if key not in selected and key not in outside_rows:
                self.issues.append({"code": "scope_exclusion_unresolved", "reference": key,
                                    "detail": "This exclusion is not a traced source amount or known outside-scope item.", "evidence": []})
        for key, (ref, item) in selected.items():
            base_only = scope["mode"] == "base_only" and item["scope_kind"] is not None
            if (base_only or key in scope["exclude"]) and item["contribution"] is not None:
                effect = -Decimal(item["contribution"])
                delta += effect
                exclusions.append({
                    "reference": ref.evidence(), "description": item["description"],
                    "source_amount": item["source_amount"], "effect": amount_text(effect),
                    "reason": "base_scope_only" if base_only else "operator_authorized_exclusion",
                })

        for key in scope["include"]:
            if key in selected and scope["mode"] != "base_only":
                continue
            candidate = outside_rows.get(key)
            if not candidate or scope["mode"] == "base_only":
                self.issues.append({"code": "scope_addition_unresolved", "reference": key,
                                    "detail": "This addition is not an eligible outside-scope amount.", "evidence": []})
                continue
            ref = Reference.from_dict(candidate["reference"])
            source_row = self.workbook.rows[ref]
            if total_kind(source_row.description, ref.sheet) is not None:
                self.issues.append({
                    "code": "scope_addition_unresolved", "reference": key,
                    "detail": "Select verified detail rows for an addition. Aggregate alternatives can overlap their children.",
                    "evidence": [ref.evidence()],
                })
                continue
            gst_basis, gst_origin = self.addition_basis(source_row, control, source_basis)
            if gst_basis is None:
                continue
            amount, markup, inputs = self.leaf(source_row)
            if amount is not None:
                delta += amount
                additions.append({
                    "reference": ref.evidence(), "description": candidate["description"],
                    "source_amount": amount_text(amount), "effect": amount_text(amount),
                    "reason": "operator_authorized_addition", "markup_treatment": markup,
                    "gst_basis": gst_basis, "gst_basis_origin": gst_origin,
                    "markup_evidence": [item.evidence() for item in inputs],
                })
        return additions, exclusions, delta


def reconcile(extraction: dict, scope: dict, answers: dict) -> dict:
    with localcontext() as context:
        context.prec = 256
        evidence = WorkbookEvidence(extraction)
        run = Reconciliation(evidence, answers)
        control = run.select_control()
        recorded = independent = difference = exact_difference = working = None
        gst_basis = basis_origin = None
        outside = additions = exclusions = []
        if control is not None:
            row = control.row
            gst_basis, basis_origin = run.source_basis(row)
            cell = evidence.cells[row.amount]
            recorded = numeric(cell)
            if recorded is None:
                run.issue("missing_reported_total", [row.amount])
            declared = cell.get("formula") is not None
            source_rows = list(evidence.detail_rows())
            omitted = {}
            if declared:
                independent = run.trace(row.amount)
            else:
                independent, omitted = run.trace_literal_total(row, source_rows)
            outside = run.check_coverage(source_rows, declared and independent is not None, omitted)
            run.check_contributing_basis(row, gst_basis)
            if independent is not None and recorded is not None:
                exact_difference = independent - recorded
                difference = cents(independent) - cents(recorded)
                if difference:
                    run.require_control_correction(row, recorded, independent)
            if independent is None and not run.questions and not run.issues:
                run.issue("missing_independent_total", [row.amount])
            additions, exclusions, delta = run.bridge(scope, outside, row, gst_basis)
            if not run.issues and not run.questions and independent is not None:
                working = cents(independent + delta)

        return {
            "status": "ready_for_classification" if working is not None else "blocked",
            "control": control.evidence() if control else None,
            "control_candidates": [candidate.evidence() for candidate in evidence.candidates],
            "gst_basis": gst_basis, "gst_basis_origin": basis_origin,
            "reported_total": amount_text(recorded), "independently_calculated_total": amount_text(independent),
            "difference": amount_text(difference), "exact_difference": amount_text(exact_difference),
            "selected_rows": list(run.selected.values()), "hierarchy": run.groups,
            "outside_source_scope": outside, "scope_additions": additions, "scope_exclusions": exclusions,
            "working_scope_total": amount_text(working),
            "issues": run.issues, "questions": run.questions,
        }
