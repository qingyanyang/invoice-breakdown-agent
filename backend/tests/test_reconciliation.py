from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from app.extraction_reader import extract_workbook
from app.reconcile import reconcile

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "invoice_breakdown"
WORKSHEET = "xl/worksheets/sheet1.xml"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SOURCE = {"mode": "source", "include": [], "exclude": []}
BASE = {"mode": "base_only", "include": [], "exclude": []}


def source_sheet(rows):
    def inline(coordinate, value):
        return f'<c r="{coordinate}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    records = [
        f'<row r="3">{inline("A3", "Description")}{inline("D3", "Amount")}</row>',
    ]
    for index, (description, amount, formula) in enumerate(rows, start=4):
        if description is None:
            records.append(f'<row r="{index}"/>')
            continue
        calculation = f"<f>{escape(formula)}</f>" if formula is not None else ""
        value = f"<v>{amount}</v>" if amount is not None else ""
        records.append(
            f'<row r="{index}">{inline(f"A{index}", description)}'
            f'<c r="D{index}">{calculation}{value}</c></row>'
        )
    return f'<worksheet xmlns="{SHEET_NS}"><sheetData>{"".join(records)}</sheetData></worksheet>'.encode()


def evidence_for(workbook_with, rows):
    return extract_workbook(workbook_with({WORKSHEET: source_sheet(rows)}))


def fixture(name):
    return extract_workbook((FIXTURES / name).read_bytes())


def test_nested_totals_trace_each_signed_leaf_and_markup():
    report = reconcile(fixture("reconciliation.xlsx"), SOURCE, {})
    assert report["status"] == "ready_for_classification"
    assert report["working_scope_total"] == "260.00" and report["difference"] == "0.00"
    assert report["control"]["reference"] == {"sheet": "Trade Summary", "cell": "D12"}
    rows = {row["reference"]["cell"]: row for row in report["selected_rows"]}
    assert set(rows) == {"D4", "D5", "D7", "D8", "D10", "D11"}
    assert [Decimal(rows[ref]["contribution"]) for ref in rows] == [
        Decimal(value) for value in ("110", "55", "60", "-10", "25", "20")
    ]
    assert rows["D4"]["markup_treatment"] == "included"
    assert {ref["cell"] for ref in rows["D4"]["markup_evidence"]} == {"B4", "C4"}
    assert {group["reference"]["cell"] for group in report["hierarchy"]} == {"D12", "D6", "D9"}


@pytest.mark.parametrize("blank_gap", [False, True])
def test_equal_and_opposite_untraced_rows_do_not_pass(workbook_with, blank_gap):
    first_omitted = 6 if blank_gap else 5
    open_rows = [(None, None, None)] if blank_gap else []
    evidence = evidence_for(workbook_with, [
        ("Base work", "75", None),
        *open_rows,
        ("Extra lining", "15", None),
        ("Door credit", "-15", None),
        ("Project Total ex GST", "75", "D4"),
    ])
    report = reconcile(evidence, SOURCE, {})
    assert report["difference"] == "0.00" and report["status"] == "blocked"
    assert report["working_scope_total"] is None
    issue, = report["issues"]
    assert issue["code"] == "untraced_source_rows"
    assert issue["evidence"] == [
        {"sheet": "Trade Summary", "cell": f"D{index}"}
        for index in (first_omitted, first_omitted + 1)
    ]
    assert [row["reference"]["cell"] for row in report["selected_rows"]] == ["D4"]


def test_options_and_signed_scope_bridge(workbook_with):
    evidence = evidence_for(workbook_with, [
        ("Base work", "100", None),
        ("Extra lining", "20", None),
        ("Lining credit", "-10", None),
        ("Option: extra area subtotal", "10", "SUM(D5:D6)"),
        ("Removal: Door credit", "-12", None),
        ("Deletion: Allowance", "5", None),
        ("Option: Unpriced initial scope alternative", "8", None),
        ("Project Total ex GST", "103", "D4+D7+D8+D9"),
    ])
    report = reconcile(evidence, BASE, {})
    assert report["status"] == "ready_for_classification"
    assert report["working_scope_total"] == "100.00"
    assert {item["reference"]["cell"]: item["effect"] for item in report["scope_exclusions"]} == {
        "D5": "-20", "D6": "10", "D8": "12", "D9": "-5",
    }
    assert [item["reference"]["cell"] for item in report["outside_source_scope"]] == ["D10"]
    authorized = reconcile(evidence, {**SOURCE, "include": ["Trade Summary!D10"]}, {})
    assert authorized["status"] == "ready_for_classification"
    assert authorized["working_scope_total"] == "111.00"
    assert authorized["scope_additions"][0]["effect"] == "8"


def test_literal_total_does_not_resolve_offsetting_options_by_sum():
    evidence = fixture("scope-ambiguity.xlsx")
    report = reconcile(evidence, SOURCE, {})
    assert report["status"] == "blocked" and report["independently_calculated_total"] is None
    assert len(report["questions"]) == 2
    assert {question["kind"] for question in report["questions"]} == {"source_inclusion"}
    answers = {question["key"]: "included" for question in report["questions"]}
    reconciled = reconcile(evidence, BASE, answers)
    assert reconciled["status"] == "ready_for_classification"
    assert reconciled["working_scope_total"] == "75.00"
    assert [item["effect"] for item in reconciled["scope_exclusions"]] == ["-15", "15"]


def test_ambiguous_project_totals_need_a_source_choice(workbook_with):
    evidence = evidence_for(workbook_with, [
        ("Base work", "75", None),
        ("Sheet Total ex GST", "75", "D4"),
        ("Option: Alternative Total", "90", "D4"),
        ("Project Total ex GST", "75", "D4"),
        ("Final Total ex GST", "75", "D4"),
        ("Project Total incl GST", "82.50", "D4"),
    ])
    report = reconcile(evidence, SOURCE, {})
    assert report["status"] == "blocked"
    question, = report["questions"]
    assert question["kind"] == "source_control"
    assert question["options"] == ["Trade Summary!D7", "Trade Summary!D8"]
    confirmed = reconcile(evidence, SOURCE, {question["key"]: "Trade Summary!D7"})
    assert confirmed["status"] == "ready_for_classification"


def test_confirmed_gst_does_not_verify_an_unsupported_formula(workbook_with):
    evidence = evidence_for(workbook_with, [
        ("Base work", "75", "POWER(5,2)*3"),
        ("Project Total", "75", "D4"),
    ])
    report = reconcile(evidence, SOURCE, {})
    question, = report["questions"]
    assert question["kind"] == "source_gst_basis"
    with_gst = reconcile(evidence, SOURCE, {question["key"]: "excluding_gst"})
    assert with_gst["status"] == "blocked"
    assert with_gst["gst_basis_origin"] == "operator_confirmed"
    assert with_gst["independently_calculated_total"] is None
    assert any(issue["code"] == "unsupported_calculation_evidence" for issue in with_gst["issues"])


def test_missing_total_cache_and_actual_discrepancy_block(workbook_with):
    missing = reconcile(fixture("missing-formula-cache.xlsx"), SOURCE, {})
    assert missing["status"] == "blocked" and missing["reported_total"] is None
    assert Decimal(missing["independently_calculated_total"]) == Decimal("260")
    mismatch = reconcile(evidence_for(workbook_with, [
        ("Base work", "75.00", None),
        ("Project Total ex GST", "74.99", "D4"),
    ]), SOURCE, {})
    assert mismatch["status"] == "blocked" and mismatch["difference"] == "0.01"
    assert mismatch["working_scope_total"] is None


def test_mixed_contributing_gst_bases_block_even_with_matching_total(workbook_with):
    evidence = evidence_for(workbook_with, [
        ("Base work", "75", None),
        ("Project Total incl GST", "75", "D4"),
        ("Final Total ex GST", "75", "D5"),
    ])
    report = reconcile(evidence, SOURCE, {})
    assert report["difference"] == "0.00"
    assert report["status"] == "blocked"
    assert any(issue["code"] == "mixed_contributing_gst_bases" for issue in report["issues"])
