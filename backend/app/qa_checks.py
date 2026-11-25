"""Check the recalculated candidate and measure coverage of its visual proof."""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.allocation import ALLOCATION_CONTEXT, AllocationPolicy, allocate
from app.reconcile import cents, reconcile
from app.reconciliation_evidence import Reference
from app.workbook_layout import ADJUSTMENT, EVIDENCE, SUMMARY, adjustment_sheet, metadata, summary_sheet
from app.workbook_source import S, SourcePackage, WorkbookProblem, parse
from app.qa_engine import EngineUnavailable
from app.qa_packages import (
    QAProblem, QABook, candidate_from_calculation, location, populated_range, render_copy, strip_formula_caches,
)

CHECKS = (
    "artifact_integrity", "recalculation", "formula_errors", "source_preservation",
    "financial_reconciliation", "preview_coverage", "owner_visual_review",
)
MACHINE_CHECKS = CHECKS[:-1]
QA_VERSION = "1"
MAX_PAGE_POINTS = 14_400
MONEY_FORMAT = re.compile(r"(?P<symbol>\$)?(?P<int>[#0,]+)(?:\.(?P<decimals>0+))?(?P<percent>%)?")
DATE_FORMATS = {
    "dd/mm/yyyy": "%d/%m/%Y", "d/m/yyyy": "%-d/%-m/%Y", "mm/dd/yyyy": "%m/%d/%Y",
    "yyyy-mm-dd": "%Y-%m-%d", "mm-dd-yy": "%m-%d-%y", "dd-mmm-yy": "%d-%b-%y",
    "d-mmm-yy": "%-d-%b-%y",
}


@dataclass(frozen=True)
class QAResult:
    checks: dict
    candidate: bytes | None = None
    preview: bytes | None = None
    coverage: list[dict] | None = None


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

        # Verify the approval's source control against the recalculated copied sheet.
        source_control = Reference.from_dict(recon["control"]["reference"])
        copied = mapping[source_control.sheet]
        observed = book.sheets[copied].cells.get(source_control.cell, {})
        try:
            raw = Decimal(observed.get("raw_value") or "NaN")
        except DecimalException:
            raw = Decimal("NaN")
        if not raw.is_finite() or cents(raw) != cents(Decimal(recon["reported_total"])):
            differences.append(location(copied, source_control.cell))
        if differences:
            raise QAProblem("Candidate amounts, labels or formulas differ from the approved workbook evidence.", differences[:200])

    return {
        "source_control": recon["reported_total"], "working_total": replay["working_total"],
        "target": replay["target"], "displayed_total": replay["display"]["total"], "difference": "0.00",
        "summary_total_cell": f"B{total_row}", "protected_total": replay["protected_total"],
    }


def text_key(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value) if not char.isspace())


def displayed_text(cell: dict, date_1904: bool) -> str | None:
    if cell["text"] is not None:
        return cell["text"]
    raw = cell["raw_value"]
    if raw in {None, ""}:
        return ""
    if cell["cell_type"] == "b":
        return "TRUE" if raw == "1" else "FALSE" if raw == "0" else None
    if cell["cell_type"] == "e":
        return raw
    code = cell["number_format"]["code"]
    if code is None or cell["cell_type"] not in {"d", "n"}:
        return None
    try:
        if pattern := DATE_FORMATS.get(code.lower()):
            if cell["cell_type"] == "d":
                value = datetime.fromisoformat(raw).date()
            else:
                days = int(Decimal(raw))
                epoch = date(1904, 1, 1) if date_1904 else date(1899, 12, 31)
                if not date_1904 and days == 60:
                    return None
                value = epoch + timedelta(days=days - (not date_1904 and days > 60))
            return value.strftime(pattern)
        if cell["cell_type"] == "d":
            return None
        value = Decimal(raw)
        if not value.is_finite():
            return None
        if code.lower() == "general":
            if abs(value) >= Decimal("1e11") or (value and abs(value) < Decimal("1e-9")):
                return None
            # Calc's XLSX numeric cache may contain binary round-trip noise.
            with localcontext() as context:
                context.prec = 15
                return format((+value).normalize(), "f") if value else "0"
        match = MONEY_FORMAT.fullmatch(code)
        if match is None:
            return None
        precision = len(match["decimals"] or "")
        percent = match["percent"] or ""
        symbol = match["symbol"] or ""
        value = (value * (100 if percent else 1)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        number = format(value.copy_abs(), f"{',' if ',' in match['int'] else ''}.{precision}f")
        return ("-" if value < 0 else "") + symbol + number + percent
    except (ValueError, OverflowError, DecimalException):
        return None


def verify_preview(book: QABook, pdf: bytes) -> list[dict]:
    try:
        pages = PdfReader(BytesIO(pdf), strict=True).pages
        if len(pages) != len(book.sheets):
            raise QAProblem("Preview page count does not cover every worksheet.")
        report, incomplete, unknown = [], [], []
        for page_no, (sheet, page) in enumerate(zip(book.sheets.values(), pages), 1):
            if not (0 < float(page.mediabox.width) <= MAX_PAGE_POINTS and 0 < float(page.mediabox.height) <= MAX_PAGE_POINTS):
                raise QAProblem("The rendered worksheet exceeds the reviewable page dimensions.", [{"sheet": sheet.name}])
            text = text_key(page.extract_text() or "")
            witnesses = []
            for ref, cell in sheet.cells.items():
                shown = displayed_text(cell, book.date_1904)
                if shown is None:
                    unknown.append(location(sheet.name, ref))
                elif shown.strip():
                    witnesses.append((ref, text_key(shown)))
            # Match longer strings first and consume each occurrence so duplicate
            # values cannot hide an omitted cell. Soft wrapping does not affect the match.
            for ref, value in sorted(witnesses, key=lambda item: -len(item[1])):
                index = text.find(value)
                if index < 0:
                    incomplete.append(location(sheet.name, ref))
                else:
                    text = text[:index] + "\x00" + text[index + len(value):]
            report.append({
                "sheet": sheet.name, "visibility": sheet.state, "page": page_no,
                "range": populated_range([ref for ref, _ in witnesses]),
                "populated_cells": len(witnesses), "note_cells": [item["coordinate"] for item in sheet.notes],
                "hidden_rows_and_columns_expanded": True,
            })
    except (PdfReadError, ValueError, OverflowError, IndexError, TypeError) as error:
        raise QAProblem("The worksheet preview could not be reopened.") from error
    if unknown:
        raise QAProblem("Some cell formats cannot be checked in the preview.", unknown[:200])
    if incomplete:
        raise QAProblem("The preview omits or clips one or more populated cells.", incomplete[:200])
    return report


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
        rendered = engine.run("render", render_copy(candidate))
        if rendered.engine != checks["recalculation"]["evidence"]["engine"]:
            raise EngineUnavailable("The calculation and preview engine versions differ.")
        coverage = verify_preview(book, rendered.data)
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
