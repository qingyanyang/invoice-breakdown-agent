"""Verify the synthetic source workbooks and independently reconcile their amounts."""

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

FIXTURES = Path(__file__).resolve().parent
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
CENT = Decimal("0.01")


def sheet_names(workbook):
    return [
        sheet.attrib["name"]
        for sheet in ElementTree.fromstring(workbook.read("xl/workbook.xml")).findall(
            "x:sheets/x:sheet", NS
        )
    ]


def cells(workbook, sheet_number):
    worksheet = ElementTree.fromstring(
        workbook.read(f"xl/worksheets/sheet{sheet_number}.xml")
    )
    return {cell.attrib["r"]: cell for cell in worksheet.findall(".//x:c", NS)}


def text(workbook, cell):
    stored = cell.findtext("x:v", namespaces=NS)
    if cell.attrib.get("t") == "str":
        return stored
    assert cell.attrib.get("t") == "s"
    strings = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    item = strings[int(stored)]
    return "".join(node.text for node in item.findall(".//x:t", NS) if node.text)


def value(cell):
    return Decimal(cell.findtext("x:v", namespaces=NS))


def cents(amount):
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def verify_reconciliation():
    with ZipFile(FIXTURES / "reconciliation.xlsx") as workbook:
        assert sheet_names(workbook) == ["Trade Summary", "Notes"]
        source = cells(workbook, 1)
        notes = cells(workbook, 2)
        assert "outside the source total" in text(workbook, notes["A3"])
        assert text(workbook, source["B2"]) == "Excluding GST"
        assert text(workbook, source["A10"]) == "Level 1 patching", "Trade Summary!A10"
        assert text(workbook, source["A11"]) == "Level 1 Standard Access Panel", (
            "Trade Summary!A11"
        )
        for coordinate, formula in {
            "D4": "B4*(1+C4)",
            "D5": "B5*(1+C5)",
            "D6": "SUM(D4:D5)",
            "D9": "SUM(D7:D8)",
            "D12": "D6+D9+D10+D11",
        }.items():
            assert source[coordinate].findtext("x:f", namespaces=NS) == formula

        partition, glazing = (
            cents(value(source[f"B{row}"]) * (1 + value(source[f"C{row}"])))
            for row in (4, 5)
        )
        doors = value(source["D7"]) + value(source["D8"])
        make_good = value(source["D10"])
        access_panel = value(source["D11"])
        assert (partition, glazing, doors) == (110, 55, 50)
        assert (make_good, access_panel) == (25, 20)
        assert cents(value(source["D4"])) == partition
        assert cents(value(source["D5"])) == glazing
        assert cents(value(source["D6"])) == partition + glazing
        assert value(source["D9"]) == doors
        source_total = partition + glazing + doors + make_good + access_panel
        assert source_total == value(source["D12"]) == 260

    target = Decimal("282.54")
    addition = Decimal("1.01")
    protected = make_good + access_panel
    factor = (target - protected - addition) / (source_total - protected)
    rounded = [cents(amount * factor) for amount in (partition, glazing, doors)]
    assert rounded == [Decimal(amount) for amount in ("121.02", "60.51", "55.01")]
    residual = target - (sum(rounded) + protected + addition)
    assert residual == Decimal("-0.01")
    assert rounded[0] > max(rounded[1:])
    rounded[0] += residual
    assert rounded[0] == Decimal("121.01")
    assert sum(rounded) + protected + addition == target


def verify_missing_formula_cache():
    worksheet_path = "xl/worksheets/sheet1.xml"
    with (
        ZipFile(FIXTURES / "reconciliation.xlsx") as original,
        ZipFile(FIXTURES / "missing-formula-cache.xlsx") as variant,
    ):
        assert set(variant.namelist()) == set(original.namelist())
        for member in original.namelist():
            if member != worksheet_path:
                assert variant.read(member) == original.read(member), member

        original_total = cells(original, 1)["D12"]
        variant_total = cells(variant, 1)["D12"]
        assert original_total.findtext("x:f", namespaces=NS) == "D6+D9+D10+D11"
        assert variant_total.findtext("x:f", namespaces=NS) == "D6+D9+D10+D11"
        assert value(original_total) == 260
        assert variant_total.find("x:v", NS) is None

        original_sheet = ElementTree.fromstring(original.read(worksheet_path))
        control = original_sheet.find(".//x:c[@r='D12']", NS)
        control.remove(control.find("x:v", NS))
        variant_sheet = ElementTree.fromstring(variant.read(worksheet_path))
        assert ElementTree.tostring(original_sheet) == ElementTree.tostring(variant_sheet)


def verify_scope_ambiguity():
    with ZipFile(FIXTURES / "scope-ambiguity.xlsx") as workbook:
        assert sheet_names(workbook) == ["Trade Summary"]
        source = cells(workbook, 1)
        assert "Combined Level 1 and Level 2" in text(workbook, source["A4"])
        assert text(workbook, source["A5"]) == "Option: Ceiling tile change", (
            "Trade Summary!A5"
        )
        assert text(workbook, source["A6"]) == "Deletion: Ceiling tile credit", (
            "Trade Summary!A6"
        )
        base, option, deletion, total = (value(source[f"D{row}"]) for row in range(4, 8))
        assert (base, option, deletion, total) == (75, 15, -15, 75)
        assert base == total == base + option + deletion


if __name__ == "__main__":
    verify_reconciliation()
    verify_scope_ambiguity()
    verify_missing_formula_cache()
    print("Verified all three synthetic workbooks and their expectations.")
