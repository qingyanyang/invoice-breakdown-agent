from pathlib import Path
from zipfile import ZipFile

import pytest

from app.features.extraction import reader

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "invoice_breakdown"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def cells(sheet):
    return {cell["coordinate"]: cell for row in sheet["rows"] for cell in row["cells"]}


def test_formulas_keep_cache_values_without_claiming_verification():
    normal = reader.extract_workbook((FIXTURES / "reconciliation.xlsx").read_bytes())
    missing = reader.extract_workbook((FIXTURES / "missing-formula-cache.xlsx").read_bytes())
    assert [sheet["name"] for sheet in normal["raw"]["sheets"]] == ["Trade Summary", "Notes"]
    original = cells(normal["raw"]["sheets"][0])
    assert original["D4"]["raw_value"] == "110.00000000000001"
    assert original["D12"]["raw_value"] == "260"
    assert original["D12"]["formula"] == {
        "text": "D6+D9+D10+D11", "attributes": {}, "cache_status": "saved_unverified",
    }
    without_cache = cells(missing["raw"]["sheets"][0])["D12"]
    assert without_cache["raw_value"] is None
    assert without_cache["formula"]["text"] == "D6+D9+D10+D11"
    assert without_cache["formula"]["cache_status"] == "missing"
    assert {"kind": "note", "source": {"sheet": "Notes", "cell": "A3"}} in normal["identified"]["annotations"]


def test_context_and_shifted_headers_keep_precise_evidence(workbook_with):
    with ZipFile(FIXTURES / "reconciliation.xlsx") as original:
        workbook = original.read("xl/workbook.xml").replace(b'name="Notes"', b'name="Notes" state="veryHidden"')
    sheet = f"""
<worksheet xmlns="{SHEET_NS}">
  <cols><col min="4" max="5" hidden="1"/></cols>
  <sheetData>
    <row r="7"><c r="F7" t="inlineStr"><is><t>Report Date</t></is></c><c r="G7" s="1"><v>46100</v></c></row>
    <row r="8"><c r="F8" t="inlineStr"><is><t>Start Date</t></is></c></row>
    <row r="11">
      <c r="B11" t="inlineStr"><is><t>Rate</t></is></c><c r="C11" t="inlineStr"><is><t>Description</t></is></c>
      <c r="D11" t="inlineStr"><is><t>Unit</t></is></c><c r="E11" t="inlineStr"><is><t>Qty</t></is></c>
      <c r="F11" t="inlineStr"><is><t>Markup %</t></is></c>
    </row>
    <row r="12" hidden="1">
      <c r="B12" s="2"><v>19.95</v></c>
      <c r="C12" t="inlineStr"><is><r><t xml:space="preserve">Wall </t></r><r><t>lining</t></r></is></c>
      <c r="D12" t="inlineStr"><is><t>m</t></is></c><c r="E12"><v>2.5</v></c><c r="F12" s="3"><v>0.125</v></c>
      <c r="G12"><f t="shared" si="0" ref="G12:G13">B12*E12</f><v>49.875</v></c>
    </row>
    <row r="13"><c r="G13"><f t="shared" si="0"/><v>0</v></c></row>
    <row r="16"><c r="C16" t="inlineStr"><is><t>Exclusions:</t></is></c>
      <c r="E16" t="inlineStr"><is><t>Client-supplied material</t></is></c></row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="H4:J4"/></mergeCells>
</worksheet>
""".encode()
    styles = f"""
<styleSheet xmlns="{SHEET_NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="$#,##0.00"/></numFmts>
<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="10"/></cellXfs>
</styleSheet>
""".encode()
    rels = f"""
<Relationships xmlns="{REL_NS}">
<Relationship Id="note" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../comments1.xml"/>
<Relationship Id="comment" Type="http://schemas.microsoft.com/office/2017/10/relationships/threadedComment" Target="../threadedComment1.xml"/>
</Relationships>
""".encode()
    notes = f"""
<comments xmlns="{SHEET_NS}"><authors><author>Analyst</author></authors><commentList>
<comment ref="C12" authorId="0"><text><r><t xml:space="preserve">Keep </t></r><r><t>exact wording.</t></r></text></comment>
</commentList></comments>
""".encode()
    threaded = b"""
<ThreadedComments xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">
<threadedComment ref="B12" id="fixture-comment" personId="fixture-user">
<text>Check the rate.</text></threadedComment></ThreadedComments>
"""
    evidence = reader.extract_workbook(workbook_with({
        "xl/workbook.xml": workbook, "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": styles, "xl/worksheets/_rels/sheet1.xml.rels": rels,
        "xl/comments1.xml": notes, "xl/threadedComment1.xml": threaded,
    }))
    first, hidden = evidence["raw"]["sheets"]
    assert hidden["visibility"] == "veryHidden"
    assert first["merged_ranges"] == ["H4:J4"]
    assert first["columns"] == [{"first": 4, "last": 5, "hidden": True}]
    assert next(row for row in first["rows"] if row["row"] == 12)["hidden"]
    raw = cells(first)
    assert raw["C12"]["text"] == "Wall lining" and raw["C12"]["cell_type"] == "inlineStr"
    assert raw["B12"]["number_format"] == {"id": 164, "code": "$#,##0.00"}
    assert raw["F12"]["number_format"] == {"id": 10, "code": "0.00%"}
    assert raw["G7"]["number_format"] == {"id": 14, "code": "mm-dd-yy"}
    assert raw["G13"]["formula"]["anchor"] == "G12" and raw["G13"]["formula"]["text"] is None
    assert raw["G13"]["formula"]["cache_status"] == "saved_unverified" and raw["G13"]["raw_value"] == "0"
    assert first["comments"][0] == {
        "coordinate": "C12", "kind": "note", "author": "Analyst", "text": "Keep exact wording.",
    }
    assert first["comments"][1]["coordinate"] == "B12"
    assert first["comments"][1]["text"] == "Check the rate."
    identified = evidence["identified"]
    assert identified["metadata"]["report_date"]["candidates"][0]["value"]["cell"] == "G7"
    assert identified["metadata"]["start_date"]["status"] == "unresolved"
    table = identified["tables"][0]
    assert table["header_row"] == 11
    assert table["rows"][0]["fields"] == {
        field: {"sheet": "Trade Summary", "cell": coordinate}
        for field, coordinate in {
            "rate": "B12", "description": "C12", "unit": "D12", "quantity": "E12", "markup": "F12",
        }.items()
    }
    assert {"kind": "exclusion", "source": {"sheet": "Trade Summary", "cell": "E16"}} in identified["annotations"]


@pytest.mark.parametrize(
    ("setting", "limit", "code"),
    [
        ("MAX_SHEETS", 1, "sheet_limit"),
        ("MAX_CELLS", 2, "cell_limit"),
        ("MAX_CONTEXT_RECORDS", 2, "context_limit"),
        ("MAX_EXPANDED_BYTES", 1_000, "expanded_limit"),
        ("MAX_RESULT_BYTES", 1_000, "result_limit"),
    ],
)
def test_limits_produce_explicit_failures(monkeypatch, setting, limit, code):
    monkeypatch.setattr(reader, setting, limit)
    with pytest.raises(reader.ExtractionProblem) as error:
        reader.extract_workbook((FIXTURES / "reconciliation.xlsx").read_bytes())
    assert error.value.code == code


def test_sparse_array_marks_only_stored_result_cells(workbook_with):
    sheet = f"""
<worksheet xmlns="{SHEET_NS}"><sheetData>
<row r="2"><c r="D2"><f t="array" ref="D2:D1048576">ROW(D2:D1048576)</f><v>2</v></c></row>
<row r="1048576"><c r="D1048576"/><c r="E1048576"><v>42</v></c></row>
</sheetData></worksheet>
""".encode()
    evidence = reader.extract_workbook(workbook_with({"xl/worksheets/sheet1.xml": sheet}))
    raw = cells(evidence["raw"]["sheets"][0])
    assert set(raw) == {"D2", "D1048576", "E1048576"}
    assert raw["D1048576"]["formula"]["anchor"] == "D2"
    assert raw["D1048576"]["formula"]["cache_status"] == "missing"
    assert raw["E1048576"]["formula"] is None
