from datetime import datetime
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from app.allocation import AllocationPolicy, allocate
from app.extraction_reader import extract_workbook
from app.reconcile import reconcile
from app.reconciliation_evidence import Reference
from app.workbook_generation import generate_workbook
from app.workbook_source import WorkbookProblem
from test_allocation_api import TRADE_BY_ROW, add_authorized_option
from test_reconciliation import SHEET_NS, SOURCE, WORKSHEET, source_sheet
from test_reconciliation_api import FIXTURES

NS = f"{{{SHEET_NS}}}"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NOTES = "xl/worksheets/sheet2.xml"


def source_parts():
    with ZipFile(FIXTURES / "reconciliation.xlsx") as file:
        return {
            name: ElementTree.fromstring(file.read(name))
            for name in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml", "xl/styles.xml")
        }


def make_workbook(payload, areas=None, *, target=None, include=()):
    extraction = extract_workbook(payload)
    scope = {**SOURCE, "include": list(include)}
    recon = reconcile(extraction, scope, {})
    assert recon["status"] == "ready_for_classification", recon
    assignments = []
    for group, amount_key, origin in (
        ("selected_rows", "contribution", "in_source"),
        ("scope_additions", "effect", "authorized_addition"),
    ):
        for item in recon[group]:
            source = Reference.from_dict(item["reference"]).key
            trade, area = (areas or {}).get(source, (TRADE_BY_ROW.get(source, "Other"), "Level 1"))
            assignments.append({
                "row": source, "trade": trade, "area": area, "amount": item[amount_key],
                "scope_origin": origin, "protected_by_default": trade in {"Make Good", "Access Panel"},
            })
    exact = sum((Decimal(item["amount"]) for item in assignments), Decimal(0))
    classified = {
        "assignments": assignments, "classified_total_exact": str(exact),
        "working_scope_total": recon["working_scope_total"], "gst_basis": recon["gst_basis"],
    }
    result = allocate(recon, classified, target, "excluding_gst" if target is not None else None, AllocationPolicy())
    assert result["status"] == "ready_for_workbook", result
    return generate_workbook(payload, extraction, recon, result)


def labelled(sheet, label):
    return next(row[1] for row in sheet.iter_rows() if row[0].value == label)


def test_targeted_workbook_opens_and_matches_approved_display(workbook_with):
    payload = add_authorized_option(workbook_with)
    result = make_workbook(payload, target=Decimal("282.54"), include=("Trade Summary!D13",))
    with BytesIO(result.data) as stream:
        formulas = load_workbook(stream)
    with BytesIO(result.data) as stream:
        values = load_workbook(stream, data_only=True)
    assert formulas.sheetnames == ["Invoice Breakdown", "Adjustment Detail", "Source Evidence", "Trade Summary", "Notes"]
    summary = values["Invoice Breakdown"]
    assert labelled(summary, "Total (ex GST)").value == 282.54
    assert labelled(summary, "Partition").value == 121.01
    assert labelled(summary, "Access Panel").value == 21.01
    assert labelled(summary, "Total (ex GST)").number_format == "$#,##0.00"
    assert labelled(formulas["Invoice Breakdown"], "Area Total").value.startswith("=ROUND(SUM(")
    assert labelled(formulas["Invoice Breakdown"], "Total (ex GST)").value.startswith("=ROUND(B")
    assert summary.column_dimensions["A"].width >= 48
    heading = next(cell for cell in formulas["Invoice Breakdown"]["A"] if cell.value == "Level 1")
    assert heading.fill.fgColor.rgb == "FFFFE49B"
    assert labelled(summary, "Total (ex GST)").fill.fgColor.rgb == "FFD8E8F8"

    detail = values["Adjustment Detail"]
    assert labelled(detail, "Displayed reconciliation difference").value == 0
    assert labelled(detail, "Protected Make Good").value == "25"
    assert Decimal(labelled(detail, "Protected Access Panel").value) == Decimal("21.01")
    residual_row = next(row for row in detail.values if row[:2] == ("Level 1", "Partition") and row[2] == -0.01)
    assert residual_row[2] == -0.01
    partition = next(row for row in detail.values if row[0] == "Trade Summary!D4")
    assert Decimal(partition[2]) == Decimal("110")
    assert labelled(detail, "Adjustment factor").value == "236.53 ÷ 215.00"
    assert partition[3] == f"{partition[2]} × (236.53 ÷ 215.00)"
    addition = next(row for row in detail.values if row[:2] == ("Trade Summary!D13", "Access Panel"))
    assert addition[2] == addition[3] == "1.01"
    assert formulas["Trade Summary"]["D4"].data_type == "f"
    assert values["Trade Summary"]["D12"].value == 260
    assert result.report["qa_status"] == "awaiting_qa"


def test_no_target_displays_real_source_rounding_difference(workbook_with):
    payload = workbook_with({WORKSHEET: source_sheet([
        ("Hoarding", "1.005", None), ("Fencing", "1.005", None),
        ("Project Total ex GST", "2.010", "SUM(D4:D5)"),
    ])})
    result = make_workbook(payload, {
        "Trade Summary!D4": ("Other", "Area A"), "Trade Summary!D5": ("Other", "Area B"),
    })
    book = load_workbook(BytesIO(result.data), data_only=True)
    assert "Adjustment Detail" not in book
    sheet = book["Invoice Breakdown"]
    assert labelled(sheet, "Total (ex GST)").value == 2.02
    assert labelled(sheet, "Reconciled source (displayed)").value == 2.01
    assert labelled(sheet, "Display rounding difference").value == 0.01
    formulas = load_workbook(BytesIO(result.data))
    assert labelled(formulas["Invoice Breakdown"], "Display rounding difference").value.startswith("=ROUND(B")
    assert result.report["display_rounding_difference"] == "0.01"
    assert not any(cell.value == "Start Date" for cell in sheet["A"])


def varied_source(workbook_with):
    parts = source_parts()
    sheets = parts["xl/workbook.xml"].find(NS + "sheets")
    sheets[1].set("name", "Invoice Breakdown")
    sheets[1].set("state", "veryHidden")
    workbook = parts["xl/workbook.xml"]
    names = ElementTree.SubElement(workbook, NS + "definedNames")
    ElementTree.SubElement(names, NS + "definedName", name="OldName").text = "'Trade Summary'!$D$4"
    refs = ElementTree.SubElement(workbook, NS + "externalReferences")
    ElementTree.SubElement(refs, NS + "externalReference", {f"{{{REL_NS}}}id": "rExternal"})
    ElementTree.SubElement(
        parts["xl/_rels/workbook.xml.rels"], f"{{{PACKAGE_REL_NS}}}Relationship",
        Id="rExternal", Type=f"{REL_NS}/externalLink", Target="externalLinks/externalLink1.xml",
    )
    content = parts["[Content_Types].xml"]
    ElementTree.SubElement(
        content, f"{{{CT_NS}}}Default", Extension="vml",
        ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing",
    )
    ElementTree.SubElement(
        content, f"{{{CT_NS}}}Override", PartName="/xl/comments1.xml",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
    )
    formats = ElementTree.SubElement(parts["xl/styles.xml"], NS + "dxfs", count="1")
    font = ElementTree.SubElement(ElementTree.SubElement(formats, NS + "dxf"), NS + "font")
    ElementTree.SubElement(font, NS + "color", rgb="FF9C0006")
    parts = {name: ElementTree.tostring(root) for name, root in parts.items()}
    parts[NOTES] = f"""
<worksheet xmlns="{SHEET_NS}" xmlns:r="{REL_NS}">
<cols><col min="7" max="7" width="21" hidden="1"/></cols><sheetData>
<row r="1" hidden="1">
<c r="A1" t="inlineStr"><is><t>Report Date</t></is></c>
<c r="B1" t="inlineStr"><is><t>2026-03-01</t></is></c><c r="C1"><v>11</v></c></row>
<row r="2">
<c r="A2" t="str"><f>'[1]Remote'!A1</f><v>Saved text</v></c>
<c r="B2"><f>OldName</f></c><c r="C2"><f>C1</f><v>11</v></c>
<c r="D2"><f>'Trade Summary'!D12</f><v>260</v></c>
<c r="E2" t="inlineStr"><is><t>=HYPERLINK("unsafe","text")</t></is></c>
<c r="F2" t="str"><f>IF(1=1,"","Text")</f><v/></c>
<c r="G2" t="str"><f>IF(1=1,"","Text")</f></c>
</row>
<row r="3">
<c r="A3" t="inlineStr"><is><t>Start Date</t></is></c>
<c r="B3" t="inlineStr"><is><t>14/03/2026</t></is></c>
<c r="C3"><f t="array" ref="C3:C4">ROW(C3:C4)</f><v>3</v></c>
</row>
<row r="4"><c r="C4"><v>4</v></c></row>
<row r="5"><c r="A5" t="inlineStr"><is><t>Original note and exclusions</t></is></c></row>
</sheetData>
<mergeCells count="1"><mergeCell ref="A5:D5"/></mergeCells>
<conditionalFormatting sqref="A1"><cfRule type="cellIs" operator="greaterThan" priority="1" dxfId="0">
<formula>0</formula></cfRule></conditionalFormatting>
<hyperlinks><hyperlink ref="A5" r:id="rLink" display="Old source" tooltip="Original workbook"/></hyperlinks>
<legacyDrawing r:id="rVml"/>
</worksheet>""".encode()
    parts["xl/worksheets/_rels/sheet2.xml.rels"] = f"""
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rNote" Type="{REL_NS}/comments" Target="../comments1.xml"/>
  <Relationship Id="rVml" Type="{REL_NS}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>
  <Relationship Id="rLink" Type="{REL_NS}/hyperlink" Target="file:///private/linked.xlsx" TargetMode="External"/>
</Relationships>""".encode()
    parts["xl/comments1.xml"] = f"""
<comments xmlns="{SHEET_NS}"><authors><author>Author</author></authors>
<commentList><comment ref="A1" authorId="0"><text><t>Audit note</t></text></comment></commentList></comments>
""".encode()
    parts["xl/drawings/vmlDrawing1.vml"] = b"""
<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:x="urn:schemas-microsoft-com:office:excel">
  <v:shape id="_x0000_s1025" style="visibility:hidden">
    <x:ClientData ObjectType="Note"><x:Row>0</x:Row><x:Column>0</x:Column></x:ClientData>
  </v:shape>
</xml>"""
    parts["xl/externalLinks/externalLink1.xml"] = f'<externalLink xmlns="{SHEET_NS}"/>'.encode()
    return workbook_with(parts)


def test_source_content_links_formulas_and_hidden_structure_survive_as_evidence(workbook_with):
    payload = varied_source(workbook_with)
    result = make_workbook(payload)
    book = load_workbook(BytesIO(result.data))
    copied = book["Invoice Breakdown 2"]
    assert copied.sheet_state == "veryHidden"
    assert copied.row_dimensions[1].hidden
    assert copied.column_dimensions["G"].hidden and copied.column_dimensions["G"].width == 21
    assert str(copied.merged_cells) == "A5:D5"
    assert (copied["A1"].comment.author, copied["A1"].comment.text) == ("Author", "Audit note")
    assert copied["A2"].value == "Saved text" and copied["A2"].data_type == "s"
    assert copied["B2"].value is None
    assert copied["C2"].value == "=C1"
    assert copied["D2"].value == 260
    assert copied["E2"].data_type == "s"
    assert copied["F2"].value == "" and copied["F2"].data_type == "s"
    assert copied["G2"].value is None
    rule, = copied.conditional_formatting["A1"]
    assert (rule.type, rule.operator, rule.formula) == ("cellIs", "greaterThan", ["0"])
    assert rule.dxf.font.color.rgb == "FF9C0006"
    assert (copied["C3"].value, copied["C4"].value) == (3, 4)
    assert copied["A5"].hyperlink is None
    assert "OldName" not in book.defined_names
    start = labelled(book["Invoice Breakdown"], "Start Date")
    assert start.value == datetime(2026, 3, 14)
    assert start.number_format == "dd/mm/yyyy"

    evidence = list(book["Source Evidence"].values)
    missing = next(row for row in evidence if row[2] == "B2")
    assert missing[4:8] == ("OldName", "", "missing", "Source snapshot")
    blank = next(row for row in evidence if row[2] == "F2")
    assert blank[4:8] == ('IF(1=1,"","Text")', "", "saved_unverified", "Source snapshot")
    uncached = next(row for row in evidence if row[2] == "G2")
    assert uncached[4:8] == ('IF(1=1,"","Text")', "", "missing", "Source snapshot")
    external = next(row for row in evidence if row[2] == "A2")
    assert external[4:8] == ("'[1]Remote'!A1", "Saved text", "saved_unverified", "Source snapshot")
    follower = next(row for row in evidence if row[2] == "C4")
    assert follower[3:7] == ("C3", "ROW(C3:C4)", "4", "saved_unverified")
    link = next(row for row in evidence if row[4] == "file:///private/linked.xlsx")
    assert link[5:7] == ("Old source", "Original workbook")
    with ZipFile(BytesIO(result.data)) as file:
        assert "xl/drawings/vmlDrawing1.vml" in file.namelist()
        assert not any("externalLink" in name for name in file.namelist())
        assert all(b'TargetMode="External"' not in file.read(name) for name in file.namelist() if name.endswith(".rels"))
    assert result.report["source_sheets"][1]["original"] == "Invoice Breakdown"
    assert result.report["source_sheets"][1]["copied"] == "Invoice Breakdown 2"


def unsupported_drawing(workbook_with):
    with ZipFile(FIXTURES / "reconciliation.xlsx") as file:
        document = ElementTree.fromstring(file.read(NOTES))
    ElementTree.SubElement(document, NS + "drawing", {f"{{{REL_NS}}}id": "rDrawing"})
    return workbook_with({NOTES: ElementTree.tostring(document)})


def test_unsupported_source_feature_reports_the_affected_sheet(workbook_with):
    with pytest.raises(WorkbookProblem) as failed:
        make_workbook(unsupported_drawing(workbook_with))
    assert failed.value.code == "unsupported_source_feature"
    assert failed.value.sheet == "Notes"
    assert failed.value.feature == "drawing"


def test_long_formula_evidence_continues_with_repeated_source_locations(workbook_with):
    formula = "CONCATENATE(" + ",".join(f'"{part:02d}' + "x" * 97 + '"' for part in range(30)) + ")"
    assert len(formula) > 3000
    payload = workbook_with({NOTES: f"""
<worksheet xmlns="{SHEET_NS}"><sheetData><row r="1">
<c r="A1" t="str"><f>{formula}</f><v>Saved result</v></c>
</row></sheetData></worksheet>
""".encode()})
    result = make_workbook(payload)
    sheet = load_workbook(BytesIO(result.data))["Source Evidence"]
    rows = [row for row in sheet.iter_rows() if row[0].value == "Notes" and row[2].value == "A1"]
    assert len(rows) >= 2
    assert "".join(row[4].value for row in rows) == formula
    assert [row[8].value for row in rows] == [f"{index}/{len(rows)}" for index in range(1, len(rows) + 1)]
    assert all(row[1].value == "Notes" for row in rows)
    width = int(sheet.column_dimensions["E"].width) - 2
    for row in rows:
        assert sheet.row_dimensions[row[4].row].height >= ((len(row[4].value) + width - 1) // width) * 15


@pytest.mark.parametrize("formula", ["'[1]Remote'!A1", "XFE1", "A1048577"])
def test_unsupported_conditional_formatting_is_rejected_with_sheet_evidence(workbook_with, formula):
    source = f"""
<worksheet xmlns="{SHEET_NS}"><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData>
<conditionalFormatting sqref="A1"><cfRule type="cellIs" operator="greaterThan" priority="1">
<formula>{formula}</formula></cfRule></conditionalFormatting></worksheet>"""
    with pytest.raises(WorkbookProblem) as failure:
        make_workbook(workbook_with({NOTES: source.encode()}))
    assert failure.value.code == "unsupported_source_feature"
    assert failure.value.sheet == "Notes"
    assert failure.value.feature == "conditional formatting formula with unsupported dependencies"
