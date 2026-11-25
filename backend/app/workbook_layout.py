"""Generate the approved review sheets from published evidence and display amounts."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from math import ceil

from lxml import etree as xml

from app.classify import Trade
from app.reconcile import cents
from app.reconciliation_evidence import Reference
from app.workbook_source import MAIN, S, XML_SPACE, WorkbookProblem, parse, serialize

SUMMARY = "Invoice Breakdown"
ADJUSTMENT = "Adjustment Detail"
EVIDENCE = "Source Evidence"
TEMPLATE_VERSION = "3"
EVIDENCE_LINES_PER_ROW = 24
MONEY = "$#,##0.00"
DATE = "dd/mm/yyyy"
NOT_STATED = "Not stated in source"
ZERO = Decimal("0.00")
EMPTY_STYLES = f'''<styleSheet xmlns="{MAIN}">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def style_extension(data: bytes | None) -> tuple[bytes, dict[str, int]]:
    root = parse(data if data is not None else EMPTY_STYLES.encode())
    if root.tag != S + "styleSheet":
        raise WorkbookProblem("invalid_style_table", "The source stylesheet could not be preserved.", feature="styles")
    if root.find(S + "numFmts") is None:
        root.insert(0, xml.Element(S + "numFmts"))
    formats, fonts, fills, xfs = (root.find(S + name) for name in ("numFmts", "fonts", "fills", "cellXfs"))
    if any(group is None or (group is not formats and not len(group)) for group in (formats, fonts, fills, xfs)):
        raise WorkbookProblem("invalid_style_table", "The source stylesheet lacks required style records.", feature="styles")
    highest_format = max((int(item.get("numFmtId")) for item in formats), default=163)
    money = max(163, highest_format) + 1
    date_format = money + 1
    for identifier, code in ((money, MONEY), (date_format, DATE)):
        xml.SubElement(formats, S + "numFmt", numFmtId=str(identifier), formatCode=code)

    bold, title = len(fonts), len(fonts) + 1
    for size in ("11", "17"):
        font = xml.SubElement(fonts, S + "font")
        xml.SubElement(font, S + "b")
        xml.SubElement(font, S + "sz", val=size)
        xml.SubElement(font, S + "color", rgb="FF172638")
        xml.SubElement(font, S + "name", val="Calibri")
    yellow, blue, header = range(len(fills), len(fills) + 3)
    for color in ("FFFFE49B", "FFD8E8F8", "FFF0F3F7"):
        pattern = xml.SubElement(xml.SubElement(fills, S + "fill"), S + "patternFill", patternType="solid")
        xml.SubElement(pattern, S + "fgColor", rgb=color)
        xml.SubElement(pattern, S + "bgColor", indexed="64")

    styles = {}
    for key, font, fill, fmt in (
        ("text", 0, 0, 0), ("title", title, 0, 0), ("label", bold, 0, 0),
        ("area", bold, yellow, 0), ("heading", bold, header, 0),
        ("money", 0, 0, money), ("total", bold, blue, 0), ("total_money", bold, blue, money),
        ("date", 0, 0, date_format), ("number", 0, 0, 0),
    ):
        styles[key] = len(xfs)
        xf = xml.SubElement(
            xfs, S + "xf", numFmtId=str(fmt), fontId=str(font), fillId=str(fill), borderId="0", xfId="0",
            applyFont="1", applyFill="1", applyNumberFormat="1", applyAlignment="1",
        )
        xml.SubElement(xf, S + "alignment", vertical="top", wrapText="1")
    for group in (formats, fonts, fills, xfs):
        group.set("count", str(len(group)))
    return serialize(root), styles


@dataclass(frozen=True)
class Cell:
    value: str | Decimal
    style: str = "text"
    formula: str | None = None


def money_cell(amount, *, total=False, formula=None):
    value = Decimal(amount)
    return Cell(value, "total_money" if total else "money", formula)


def alphabet(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


class Worksheet:
    def __init__(self, name, styles, widths, *, selected=False):
        self.name = name
        self.styles = styles
        self.widths = widths
        self.row_number = 0
        self.root = xml.Element(S + "worksheet", nsmap={None: MAIN})
        views = xml.SubElement(self.root, S + "sheetViews")
        xml.SubElement(views, S + "sheetView", workbookViewId="0", showGridLines="0", tabSelected="1" if selected else "0")
        xml.SubElement(self.root, S + "sheetFormatPr", defaultRowHeight="16")
        columns = xml.SubElement(self.root, S + "cols")
        for column, width in enumerate(widths, 1):
            xml.SubElement(columns, S + "col", min=str(column), max=str(column), width=str(width), customWidth="1")
        self.rows = xml.SubElement(self.root, S + "sheetData")

    def row(self, *values: Cell | str | None) -> int:
        self.row_number += 1
        if self.row_number > 1_048_576:
            raise WorkbookProblem("output_limit", "Review sheet exceeds the row limit.", sheet=self.name)
        row = xml.SubElement(self.rows, S + "row", r=str(self.row_number))
        line_count = 1
        for index, value in enumerate(values, 1):
            if value is None:
                continue
            cell = value if isinstance(value, Cell) else Cell(value)
            coordinate = alphabet(index) + str(self.row_number)
            node = xml.SubElement(row, S + "c", r=coordinate, s=str(self.styles[cell.style]))
            if isinstance(cell.value, Decimal):
                self._number(node, cell, coordinate)
            else:
                if len(cell.value.encode("utf-16-le")) // 2 > 32767:
                    raise WorkbookProblem(
                        "output_cell_limit", "Full source trace text exceeds Excel's cell limit.",
                        sheet=self.name, feature=coordinate,
                    )
                node.set("t", "inlineStr")
                text = xml.SubElement(xml.SubElement(node, S + "is"), S + "t", {XML_SPACE: "preserve"})
                text.text = cell.value
                width = max(1, self.widths[index - 1] - 2)
                line_count = max(line_count, sum(max(1, ceil(len(line) / width)) for line in cell.value.split("\n")))
        height = min(409, max(30 if any(isinstance(c, Cell) and c.style == "title" for c in values) else 16, line_count * 15))
        row.attrib.update({"ht": str(height), "customHeight": "1"})
        return self.row_number

    def _number(self, node, cell, coordinate):
        # Excel cannot preserve more than 15 significant digits as a numeric cell.
        digits = len(cell.value.normalize().as_tuple().digits)
        if not cell.value.is_finite() or digits > 15:
            raise WorkbookProblem(
                "excel_numeric_precision", "The exact numeric value exceeds Excel's supported precision.",
                sheet=self.name, feature=coordinate,
            )
        if cell.formula is not None:
            xml.SubElement(node, S + "f").text = cell.formula
        xml.SubElement(node, S + "v").text = format(cell.value, "f")

    def render(self) -> bytes:
        return serialize(self.root)


def metadata(extraction: dict) -> tuple[dict[str, str | Decimal | None], list[tuple]]:
    found = extraction["identified"]["metadata"]
    values = {}
    evidence = []
    source_cells = {
        Reference(sheet["name"], cell["coordinate"]): cell
        for sheet in extraction["raw"]["sheets"] for row in sheet["rows"] for cell in row["cells"]
    }
    for name in ("project_name", "project_address", "start_date"):
        entry = found.get(name, {})
        if entry.get("status") != "labelled":
            continue
        candidate, = entry["candidates"]
        ref = Reference.from_dict(candidate["value"])
        cell = source_cells[ref]
        if cell["formula"]:
            continue
        value = (cell["text"] or "").strip()
        if name == "start_date":
            value = date_serial(cell, extraction["raw"]["date_system"])
        if value not in {None, ""}:
            values[name] = value
            evidence.append((name, ref.sheet, ref.cell))
    return values, evidence


def date_serial(cell, date_system: str) -> Decimal | None:
    epoch_1904 = date_system == "1904"
    if cell["cell_type"] == "n" and cell["raw_value"]:
        code = cell["number_format"]["code"] or ""
        symbols = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code)
        if re.search(r"(?i)(?:y{2,4}|d{1,4})", symbols):
            try:
                number = Decimal(cell["raw_value"])
            except InvalidOperation:
                return None
            limit = 2957003 if epoch_1904 else 2958465
            if number.is_finite() and number == number.to_integral_value() and 0 <= number <= limit:
                if not epoch_1904 and number in {0, 60}:
                    return None
                return number
    value = cell["text"] or (cell["raw_value"] if cell["cell_type"] == "d" else "")
    if not value:
        return None
    try:
        confirmed = date.fromisoformat(value[:10]) if re.fullmatch(r"\d{4}-\d\d-\d\d(?:T00:00:00(?:Z)?)?", value) else (
            datetime.strptime(value, "%d/%m/%Y").date() if re.fullmatch(r"\d\d/\d\d/\d{4}", value) else None
        )
    except ValueError:
        return None
    if confirmed is None:
        return None
    epoch = date(1904, 1, 1) if epoch_1904 else date(1899, 12, 31)
    number = (confirmed - epoch).days
    if number < (0 if epoch_1904 else 1):
        return None
    return Decimal(number + (not epoch_1904 and confirmed >= date(1900, 3, 1)))


def check_display(report: dict):
    display = report["display"]
    area_totals = defaultdict(lambda: ZERO)
    trade_totals = defaultdict(lambda: ZERO)
    values = {}
    for cell in display["cells"]:
        area, trade = cell["area"], Trade(cell["trade"])
        value = Decimal(cell["display_total"])
        if not value.is_finite() or value != cents(value) or (area, trade) in values:
            raise WorkbookProblem("allocation_display_mismatch", "Allocation display cells are incomplete or inconsistent.")
        values[area, trade] = value
        area_totals[area] += value
        trade_totals[trade] += value
    total = Decimal(display["total"])
    rounding = display["rounding"]
    comparison = Decimal(rounding["comparison_total"])
    difference = Decimal(rounding["difference"])
    targeted = report["mode"] == "target_allocation"
    expected = Decimal(report["target"]) if targeted else cents(Decimal(report["working_total"]))
    if (
        dict(area_totals) != {key: Decimal(value) for key, value in display["area_totals"].items()}
        or dict(trade_totals) != {key: Decimal(value) for key, value in display["trade_totals"].items()}
        or sum(values.values(), ZERO) != total or comparison != expected
        or total - comparison != difference or (targeted and difference)
    ):
        raise WorkbookProblem("allocation_display_mismatch", "Allocation display totals do not match the approved report.")
    return values, area_totals, total, comparison, difference


def summary_sheet(styles, report, fields):
    values, areas, total, comparison, difference = check_display(report)
    basis = report["source_gst_basis"]
    if basis not in {"excluding_gst", "including_gst"}:
        raise WorkbookProblem("unconfirmed_display_gst", "The source GST basis is not confirmed.")
    sheet = Worksheet(SUMMARY, styles, (52, 23), selected=True)
    sheet.row(Cell(SUMMARY, "title"))
    sheet.row("Generated for review · Awaiting QA")
    if "start_date" in fields:
        sheet.row(Cell("Start Date", "label"), Cell(fields["start_date"], "date"))
    for label, key in (("Job Name", "project_name"), ("Job Address", "project_address")):
        sheet.row(Cell(label, "label"), fields.get(key, NOT_STATED))
    sheet.row()
    area_refs = []
    for area, amount in sorted(areas.items()):
        sheet.row(Cell(area, "area"), Cell("Amount", "area"))
        first = sheet.row_number + 1
        for trade in Trade:
            sheet.row(trade, money_cell(values.get((area, trade), ZERO)))
        end = sheet.row_number
        row = sheet.row(Cell("Area Total", "total"), money_cell(amount, total=True, formula=f"ROUND(SUM(B{first}:B{end}),2)"))
        area_refs.append(f"B{row}")
        sheet.row()
    if not area_refs:
        row = sheet.row("No working-scope items", money_cell(ZERO))
        area_refs.append(f"B{row}")
    label = "Total (ex GST)" if basis == "excluding_gst" else "Total (inc GST)"
    total_row = sheet.row(Cell(label, "total"), money_cell(total, total=True, formula=f"ROUND({'+'.join(area_refs)},2)"))
    if report["mode"] == "no_contract_price_adjustment":
        comparison_row = sheet.row("Reconciled source (displayed)", money_cell(comparison))
        sheet.row(
            "Display rounding difference",
            money_cell(difference, formula=f"ROUND(B{total_row}-B{comparison_row},2)"),
        )
    return sheet, total_row


def exact_text(amount) -> str:
    return "—" if amount is None else str(amount)


def adjustment_sheet(styles, report, reconciliation, summary_total_row):
    sheet = Worksheet(ADJUSTMENT, styles, (35, 25, 20, 34, 20, 44))
    sheet.row(Cell(ADJUSTMENT, "title"))
    sheet.row("Generated for review · Awaiting QA")
    for label, key in (
        ("Original source total", "reported_total"),
        ("Working-scope source total", "working_total"),
        ("Protected amounts from source", "protected_in_source"),
        ("Authorized protected additions", "protected_authorized_additions"),
        ("Protected total", "protected_total"),
        ("Adjustable base", "adjustable_base"),
    ):
        sheet.row(Cell(label, "label"), exact_text(reconciliation.get(key) if key == "reported_total" else report[key]))
    protected_by_trade = defaultdict(Decimal)
    for item in report["assignments"]:
        if item["protection"] is not None:
            protected_by_trade[item["trade"]] += Decimal(item["source_amount"])
    for trade in (Trade.MAKE_GOOD, Trade.ACCESS_PANEL):
        sheet.row(Cell(f"Protected {trade}", "label"), exact_text(protected_by_trade.pop(trade, ZERO)))
    if protected_by_trade:
        sheet.row(Cell("Other authorized protected items", "label"), exact_text(sum(protected_by_trade.values(), ZERO)))
    target_row = sheet.row(Cell("Authorized target (ex GST)", "label"), money_cell(report["target"]))
    factor = report.get("factor")
    factor_text = f"{factor['numerator']} ÷ {factor['denominator']}" if factor else "No factor applied"
    sheet.row(Cell("Adjustment factor", "label"), factor_text)
    difference = Decimal(report["display"]["rounding"]["difference"])
    sheet.row(
        Cell("Displayed reconciliation difference", "total"),
        money_cell(difference, total=True, formula=f"ROUND('{SUMMARY}'!B{summary_total_row}-B{target_row},2)"),
    )
    for scope, label in (("scope_additions", "Authorized scope additions"), ("scope_exclusions", "Authorized scope exclusions")):
        if reconciliation[scope]:
            sheet.row()
            sheet.row(Cell(label, "heading"))
            for item in reconciliation[scope]:
                sheet.row(Reference.from_dict(item["reference"]).key, item["description"], exact_text(item["effect"]))

    sheet.row()
    sheet.row(*(Cell(value, "heading") for value in ("Area", "Trade", "Original", "Adjusted", "Difference", "Rule")))
    for cell in report["display"]["cells"]:
        original = Decimal(cell["protected_exact"]) + Decimal(cell["adjustable_base"])
        adjusted = Decimal(cell["display_total"])
        row = sheet.row_number + 1
        sheet.row(
            cell["area"], cell["trade"], money_cell(cents(original)), money_cell(adjusted),
            money_cell(adjusted - cents(original), formula=f"ROUND(D{row}-C{row},2)"),
            f"Protected display {cell['protected_display']}; adjustable display {cell['adjustable_display']}",
        )
    recipients = report["display"]["rounding"]["recipients"]
    if recipients:
        sheet.row()
        sheet.row(Cell("Rounding residual applied", "heading"))
        for item in recipients:
            sheet.row(item["area"], item["trade"], money_cell(item["amount"]))

    sheet.row()
    sheet.row(*(Cell(value, "heading") for value in ("Source row", "Trade", "Original exact", "Adjusted exact", "Scope", "Rule")))
    for item in report["assignments"]:
        adjusted = item["unrounded_amount"]
        if factor and item["protection"] is None:
            # Express recurring amounts exactly without expanding them across hundreds of digits.
            adjusted = f"{item['source_amount']} × ({factor_text})"
        sheet.row(
            item["row"], item["trade"], item["source_amount"], adjusted,
            item["scope_origin"], item["protection"] or "Uniform factor",
        )
    return sheet


def evidence_fragments(value: str, width: int):
    """Split while preserving all original characters, including newlines."""
    start = occupied = 0
    lines = 1
    for index, char in enumerate(value):
        if char == "\n" or occupied == width:
            if lines == EVIDENCE_LINES_PER_ROW:
                yield value[start:index]
                start = index
                lines = 2 if char == "\n" else 1
            else:
                lines += 1
            occupied = 0
            if char == "\n":
                continue
        occupied += 1
    yield value[start:]


def evidence_rows(sheet: Worksheet, values: tuple, split_columns: tuple[int, ...]) -> None:
    fragments = list(zip_longest(*(
        evidence_fragments(values[column], sheet.widths[column] - 2) for column in split_columns
    )))
    for part, cells in enumerate(fragments, 1):
        row = list(values)
        for column, value in zip(split_columns, cells):
            row[column] = value
        sheet.row(*row, f"{part}/{len(fragments)}" if len(fragments) > 1 else None)


def evidence_sheet(styles, package, metadata_evidence):
    sheet = Worksheet(EVIDENCE, styles, (26, 28, 20, 18, 65, 24, 24, 28, 12))
    sheet.row(Cell(EVIDENCE, "title"))
    sheet.row("Source sheets in original order. Saved results retain their original verification status.")
    sheet.row(*(Cell(value, "heading") for value in ("Source sheet", "Copied tab", "Visibility")))
    for item in package.sheets:
        sheet.row(item.before["name"], item.output_name, item.before["visibility"])
    if metadata_evidence:
        sheet.row()
        sheet.row(*(Cell(value, "heading") for value in ("Confirmed field", "Source sheet", "Cell")))
        for item in metadata_evidence:
            sheet.row(*item)
    if package.formulas:
        sheet.row()
        sheet.row(*(Cell(value, "heading") for value in (
            "Source sheet", "Copied tab", "Cell", "Formula anchor", "Original formula",
            "Saved value", "Cache status", "Handling", "Part",
        )))
        for item in package.formulas:
            evidence_rows(sheet, item, (4, 5))
    if package.names or package.links:
        sheet.row()
        sheet.row(*(Cell(value, "heading") for value in (
            "Scope", "Cell or name", "Handling", "", "Original reference", "Display text", "Tooltip", "", "Part",
        )))
        for source, ref, text in package.names:
            evidence_rows(sheet, (source, ref, "Named range snapshot", None, text, None, None, None), (4,))
        for source, ref, text, display, tooltip in package.links:
            evidence_rows(sheet, (source, ref, "Link snapshot", None, text, display, tooltip, None), (4, 5, 6))
    return sheet
