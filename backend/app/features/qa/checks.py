"""Check the recalculated candidate and measure coverage of its visual proof."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from io import BytesIO
from zipfile import BadZipFile, ZipFile


from app.features.allocation.calculate import ALLOCATION_CONTEXT, AllocationPolicy, allocate
from app.money import cents
from app.features.reconciliation.calculate import reconcile
from app.features.extraction.references import Reference
from app.features.reconciliation.evidence import numeric
from app.features.workbooks.layout import ADJUSTMENT, EVIDENCE, SUMMARY, adjustment_sheet, metadata, summary_sheet
from app.features.workbooks.source import S, SourcePackage, WorkbookProblem, parse
from app.features.qa.engine import EngineUnavailable
from app.features.qa.preview import measure_copy, preview_witnesses, verify_preview
from app.features.qa.packages import (
    QAProblem, QABook, candidate_from_calculation, location, render_copy, strip_formula_caches,
)

CHECKS = (
    "artifact_integrity", "recalculation", "formula_errors", "source_preservation",
    "financial_reconciliation", "preview_coverage", "owner_visual_review",
)
MACHINE_CHECKS = CHECKS[:-1]
QA_VERSION = "2"


@dataclass(frozen=True)
class QAResult:
    checks: dict
    candidate: bytes | None = None
    preview: bytes | None = None
    coverage: list[dict] | None = None
    engine_changed: bool = False


def outcome(status: str, detail: str, *, locations=None, evidence=None):
    return {"status": status, "detail": detail, "locations": locations or [], "evidence": evidence or {}}


def default_checks():
    return {name: outcome("not_performed", "This check has not run.") for name in CHECKS}


def record_failure(checks, name, problem, *, unavailable=False):
    checks[name] = outcome(
        "not_performed" if unavailable else "failed", str(problem), locations=getattr(problem, "locations", []),
    )


def actual_errors(book):
    return [
        {**location(sheet.name, ref), "value": cell["raw_value"]}
        for sheet in book.sheets.values() for ref, cell in sheet.cells.items()
        if cell["cell_type"] == "e" and cell["raw_value"]
    ]


def source_preservation(source: bytes, review: bytes, inputs, mapped, candidate: QABook):
    try:
        with ZipFile(BytesIO(source)) as original, ZipFile(BytesIO(review)) as generated:
            package = SourcePackage(original, inputs["extraction"], {name.casefold() for name in (SUMMARY, ADJUSTMENT, EVIDENCE)})
            package.verify_source_copy(generated)
            mapping = {item.before["name"]: item.output_name for item in package.sheets}
            if mapping != mapped or any(name not in candidate.sheets for name in mapping.values()):
                raise QAProblem("Candidate source-sheet mapping differs from the verified source.")
    except (BadZipFile, WorkbookProblem, ValueError, LookupError) as error:
        locations = [{"sheet": error.sheet}] if getattr(error, "sheet", None) else []
        raise QAProblem("Source-sheet fidelity could not be confirmed.", locations) from error
    # Literal cells, original formula expressions, and geometry are also checked against
    # the exact candidate package by candidate_from_calculation before this check runs.
    return {"source_sheets": mapping, "candidate_changes": "formula_caches_and_review_banner_only"}


def template_values(sheet):
    wanted = {}
    for node in parse(sheet.render()).iter(S + "c"):
        ref = node.get("r")
        if ref == "A2":
            continue
        value = node.findtext(S + "v")
        if value is None:
            value = node.findtext(f"{S}is/{S}t") or ""
        else:
            value = Decimal(value)
        wanted[ref] = (node.findtext(S + "f"), value)
    return wanted


def traced_source_amounts(recon: dict) -> dict[Reference, Decimal]:
    # Use each cell's own sign. Parent coefficients apply only when rolling up a subtotal.
    amounts = {
        Reference.from_dict(item["reference"]): Decimal(item["source_amount"])
        for item in (*recon["selected_rows"], *recon["scope_additions"])
    }
    # Reconciliation records groups before descending into their children.
    for group in reversed(recon["hierarchy"]):
        amounts[Reference.from_dict(group["reference"])] = sum(
            child["coefficient"] * amounts[Reference.from_dict(child["reference"])]
            for child in group["children"]
        )
    amounts[Reference.from_dict(recon["control"]["reference"])] = Decimal(recon["independently_calculated_total"])
    return amounts


def finance_check(inputs, book: QABook, mapping: dict):
    with localcontext(ALLOCATION_CONTEXT):
        current = inputs["reconciliation"]
        allocation = inputs["allocation"]
        recon = reconcile(inputs["extraction"], inputs["scope"], inputs["scope_answers"])
        if recon != current:
            raise QAProblem("Replaying the reconciliation did not reproduce the approved scope and control totals.")
        replay = allocate(
            recon, inputs["classification"], inputs["target"], inputs["gst_basis"], AllocationPolicy(**inputs["policy"]),
        )
        if replay["status"] != "ready_for_workbook" or any(allocation.get(key) != value for key, value in replay.items()):
            raise QAProblem("Replaying the allocation did not reproduce the approved amounts, protection and rounding.")
        difference = Decimal(replay["display"]["rounding"]["difference"])
        if difference != 0:
            raise QAProblem("The displayed reconciliation difference must be zero before delivery.", [{"sheet": SUMMARY}])
        if SUMMARY not in book.sheets:
            raise QAProblem("The candidate is missing the Invoice Breakdown sheet.")

        # Workbook generators share layout addresses, while the required values come
        # from the replay above and are compared with the actual candidate cells.
        styles = defaultdict(int)
        fields, _ = metadata(inputs["extraction"])
        summary, total_row = summary_sheet(styles, replay, fields)
        reports = [summary]
        if replay["mode"] == "target_allocation":
            reports.append(adjustment_sheet(styles, replay, recon, total_row))
        differences = []
        for expected in reports:
            actual_sheet = book.sheets.get(expected.name)
            if actual_sheet is None:
                differences.append({"sheet": expected.name})
                continue
            expected_cells = template_values(expected)
            observed = {ref: cell for ref, cell in actual_sheet.cells.items()
                        if cell["formula"] is not None or cell["raw_value"] is not None or cell["text"] is not None}
            for ref in observed.keys() ^ expected_cells.keys():
                differences.append(location(expected.name, ref))
            for ref in observed.keys() & expected_cells.keys():
                formula, value = expected_cells[ref]
                actual = observed[ref]
                actual_formula = actual["formula"]["text"] if actual["formula"] else None
                if formula != actual_formula:
                    differences.append(location(expected.name, ref))
                elif isinstance(value, Decimal):
                    raw = actual["raw_value"]
                    try:
                        measured = Decimal(raw) if raw is not None else None
                    except DecimalException:
                        measured = None
                    # Computed currency cells are compared at their displayed cent precision.
                    if measured is None or not measured.is_finite() or (cents(measured) if formula else measured) != value:
                        differences.append(location(expected.name, ref))
                elif (actual["text"] or "") != value:
                    differences.append(location(expected.name, ref))

        source_amounts = traced_source_amounts(recon)
        for ref, expected in source_amounts.items():
            copied = mapping.get(ref.sheet, ref.sheet)
            sheet = book.sheets.get(copied)
            observed = numeric(sheet.cells.get(ref.cell)) if sheet and ref.sheet in mapping else None
            if observed is None or cents(observed) != cents(expected):
                differences.append(location(copied, ref.cell))
        if differences:
            raise QAProblem("Candidate amounts, labels or formulas differ from the approved workbook evidence.", differences[:200])

    return {
        "source_control": recon["reported_total"], "working_total": replay["working_total"],
        "target": replay["target"], "displayed_total": replay["display"]["total"], "difference": "0.00",
        "summary_total_cell": f"B{total_row}", "protected_total": replay["protected_total"],
        "verified_source_amount_cells": len(source_amounts),
    }


def inspect_workbook(source: bytes, review: bytes, inputs: dict, mapped: dict, engine) -> QAResult:
    checks = default_checks()
    checks["artifact_integrity"] = outcome(
        "passed", "The source and review workbook were read with their recorded size and checksum.",
    )
    try:
        uncached, formula_count = strip_formula_caches(review)
        calculation = engine.run("calculate", uncached)
    except EngineUnavailable as error:
        record_failure(checks, "recalculation", error, unavailable=True)
        return QAResult(checks)
    except QAProblem as error:
        record_failure(checks, "recalculation", error)
        return QAResult(checks)
    try:
        candidate = candidate_from_calculation(review, calculation.data)
        book = QABook(candidate)
    except QAProblem as error:
        record_failure(checks, "recalculation", error)
        return QAResult(checks)

    checks["recalculation"] = outcome(
        "passed", "Fresh results were imported for every original active formula.",
        evidence={"engine": calculation.engine, "formula_count": formula_count},
    )
    errors = actual_errors(book)
    checks["formula_errors"] = (
        outcome("failed", "Actual workbook error cells were found.", locations=errors[:200], evidence={"count": len(errors)})
        if errors else outcome("passed", "Every worksheet was scanned for actual Excel error cells.")
    )
    try:
        evidence = source_preservation(source, review, inputs, mapped, book)
        checks["source_preservation"] = outcome("passed", "Source cells, formulas and worksheet structure were preserved.", evidence=evidence)
    except QAProblem as error:
        record_failure(checks, "source_preservation", error)
    try:
        totals = finance_check(inputs, book, mapped)
        checks["financial_reconciliation"] = outcome("passed", "Candidate values match the approved accounting evidence.", evidence=totals)
    except (WorkbookProblem, QAProblem) as error:
        record_failure(checks, "financial_reconciliation", error)
    if any(checks[name]["status"] != "passed" for name in MACHINE_CHECKS[:-1]):
        return QAResult(checks, candidate)

    return review_preview(candidate, checks, engine)


def review_preview(candidate: bytes, checks: dict, engine) -> QAResult:
    checks["owner_visual_review"] = outcome("not_performed", "The exact candidate previews require owner visual review.")
    try:
        book = QABook(candidate)
        visible = render_copy(candidate)
        rendered = engine.run("render", visible)
        if rendered.engine != checks["recalculation"]["evidence"]["engine"]:
            checks["preview_coverage"] = outcome(
                "not_performed", "The calculation and preview engine versions differ.",
            )
            return QAResult(checks, candidate, engine_changed=True)
        witnesses = preview_witnesses(book)
        layout, colors = measure_copy(visible, witnesses)
        measured = engine.run("measure", layout)
        if rendered.engine != measured.engine:
            raise EngineUnavailable("The preview and layout engine versions differ.")
        coverage = verify_preview(book, rendered.data, measured.data, colors, witnesses)
    except EngineUnavailable as error:
        record_failure(checks, "preview_coverage", error, unavailable=True)
        return QAResult(checks, candidate)
    except QAProblem as error:
        record_failure(checks, "preview_coverage", error)
        return QAResult(checks, candidate)

    checks["preview_coverage"] = outcome(
        "passed", "Rendered pages cover the populated cells of every worksheet.",
        evidence={"engine": rendered.engine, "worksheets": coverage},
    )
    return QAResult(checks, candidate, rendered.data, coverage)
