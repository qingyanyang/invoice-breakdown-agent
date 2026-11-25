"""Build a review workbook without applying calculations to source evidence."""

from dataclasses import dataclass
from decimal import DecimalException, localcontext
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException
from lxml import etree as xml

from app.allocation import ALLOCATION_CONTEXT
from app.extraction_reader import ExtractionProblem
from app.workbook_layout import (
    ADJUSTMENT, EVIDENCE, SUMMARY, TEMPLATE_VERSION, adjustment_sheet, evidence_sheet, metadata, style_extension,
    summary_sheet,
)
from app.workbook_source import SourcePackage, WorkbookProblem
from app.workbook_upload import validate_workbook

GENERATOR_VERSION = "2"
REVIEW_STATUS = "generated_awaiting_qa"


@dataclass(frozen=True)
class GeneratedWorkbook:
    data: bytes
    project_name: str | None
    report: dict


def generate_workbook(source: bytes, extraction: dict, reconciliation: dict, allocation: dict) -> GeneratedWorkbook:
    try:
        validate_workbook(source)
        with ZipFile(BytesIO(source)) as original, localcontext(ALLOCATION_CONTEXT):
            reserved = {name.casefold() for name in (SUMMARY, ADJUSTMENT, EVIDENCE)}
            package = SourcePackage(original, extraction, reserved)
            styles_xml, styles = style_extension(package.parts.get(package.style_part))
            package.install_styles(styles_xml)
            fields, evidence = metadata(extraction)
            summary, total_row = summary_sheet(styles, allocation, fields)
            output_sheets = [summary]
            if allocation["mode"] == "target_allocation":
                output_sheets.append(adjustment_sheet(styles, allocation, reconciliation, total_row))
            output_sheets.append(evidence_sheet(styles, package, evidence))
            for index, sheet in enumerate(output_sheets):
                package.add_sheet(index, sheet.name, sheet.render())
            result = package.finish()
            report = {
                "source_sheets": [{
                    "original": item.before["name"], "copied": item.output_name, "visibility": item.before["visibility"],
                    "retained_formulas": item.formula_count - item.snapshot_count,
                    "snapshot_formulas": item.snapshot_count,
                } for item in package.sheets],
                "source_formula_cache_status": "unchanged_verification",
                "generated_sheets": [sheet.name for sheet in output_sheets],
                "copied_source_fidelity": "structural_and_cell_evidence_matched",
                "display_rounding_difference": allocation["display"]["rounding"]["difference"],
                "qa_status": "awaiting_qa",
            }
            return GeneratedWorkbook(result, fields.get("project_name"), report)
    except WorkbookProblem:
        raise
    except (BadZipFile, HTTPException, ExtractionProblem, xml.LxmlError, ValueError, TypeError, LookupError,
            OverflowError, DecimalException) as error:
        raise WorkbookProblem("workbook_generation_failed", "The source workbook or allocation could not be rendered safely.") from error
