"""Prove that each workbook cell's text is actually painted in its preview region."""

import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from io import BytesIO
from math import isfinite

from lxml import etree as xml
from pdfminer.layout import LTChar, LTComponent
from pdfminer.pdfdevice import PDFTextDevice
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdffont import PDFUnicodeNotDefined
from pdfminer.psexceptions import PSException
from pdfminer.utils import Plane, apply_matrix_pt

from app.qa_packages import QABook, QAProblem, location, populated_range
from app.workbook_source import S, parse, serialize

MAX_PAGE_POINTS = 14_400
MAX_STYLES = 65_490
MARKER_START = 0x110001
# The renderer rounds PDF coordinates to thousandths of a point and clips its outer margins.
PDF_TOLERANCE = 0.15
MONEY_FORMAT = re.compile(r"(?P<symbol>\$)?(?P<int>[#0,]+)(?:\.(?P<decimals>0+))?(?P<percent>%)?")
DATE_FORMATS = {
    "dd/mm/yyyy": "%d/%m/%Y", "d/m/yyyy": "%-d/%-m/%Y", "mm/dd/yyyy": "%m/%d/%Y",
    "yyyy-mm-dd": "%Y-%m-%d", "mm-dd-yy": "%m-%d-%y", "dd-mmm-yy": "%d-%b-%y",
    "d-mmm-yy": "%-d-%b-%y",
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


def preview_witnesses(book):
    witnesses = {}
    unknown = []
    for sheet in book.sheets.values():
        cells = {}
        for ref, cell in sheet.cells.items():
            shown = displayed_text(cell, book.date_1904)
            if shown is None:
                unknown.append(location(sheet.name, ref))
            elif shown.strip():
                cells[ref] = text_key(shown)
        witnesses[sheet.name] = cells
    if unknown:
        raise QAProblem("Some cell formats cannot be checked in the preview.", unknown[:200])
    return witnesses


def measure_copy(data, witnesses):
    """Render a disposable copy with uniquely colored cell backgrounds for geometry."""
    book = QABook(data)
    styles = parse(book.parts[book.style_part])
    fills, formats = (styles.find(S + name) for name in ("fills", "cellXfs"))
    count = sum(map(len, witnesses.values()))
    if len(formats) + count > MAX_STYLES:
        raise QAProblem("The preview exceeds the supported number of measurable cell styles.")
    for style_group in (formats, styles.find(S + "cellStyleXfs")):
        if style_group is not None:
            for style in style_group:
                style.set("fillId", "0")

    colors = {}
    marker = MARKER_START
    for name, sheet in book.sheets.items():
        colors[name] = {}
        for rule in sheet.root.findall(S + "conditionalFormatting"):
            sheet.root.remove(rule)
        for ref in witnesses[name]:
            color = f"{marker:06X}"
            colors[name][marker] = ref
            marker += 1
            fill = xml.SubElement(fills, S + "fill")
            pattern = xml.SubElement(fill, S + "patternFill", patternType="solid")
            xml.SubElement(pattern, S + "fgColor", rgb="FF" + color)
            xml.SubElement(pattern, S + "bgColor", indexed="64")
            node = sheet.nodes[ref]
            style = deepcopy(formats[int(node.get("s", "0"))])
            style.attrib.update({"fillId": str(len(fills) - 1), "applyFill": "1"})
            node.set("s", str(len(formats)))
            formats.append(style)

    fills.set("count", str(len(fills)))
    formats.set("count", str(len(formats)))
    book.parts[book.style_part] = serialize(styles)
    return book.dump(book.sheets.values()), colors


def rectangle(path, matrix):
    # Calc draws fills and clips as closed, axis-aligned rectangular paths.
    if [segment[0] for segment in path] != ["m", "l", "l", "l", "h"]:
        return None
    corners = [apply_matrix_pt(matrix, segment[1:]) for segment in path[:-1]]
    if any(x1 != x2 and y1 != y2 for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1])):
        return None
    points = set(corners)
    left, bottom = (min(axis) for axis in zip(*points))
    right, top = (max(axis) for axis in zip(*points))
    if len(points) != 4 or points != {(left, bottom), (left, top), (right, bottom), (right, top)}:
        return None
    return left, bottom, right, top


def intersection(first, second):
    if first is None or second is None:
        return None
    left, bottom = max(first[0], second[0]), max(first[1], second[1])
    right, top = min(first[2], second[2]), min(first[3], second[3])
    return (left, bottom, right, top) if left < right and bottom < top else None


def contains(outer, inner):
    return outer is not None and (
        outer[0] - PDF_TOLERANCE <= inner[0] <= inner[2] <= outer[2] + PDF_TOLERANCE
        and outer[1] - PDF_TOLERANCE <= inner[1] <= inner[3] <= outer[3] + PDF_TOLERANCE
    )


class PreviewInterpreter(PDFPageInterpreter):
    def do_q(self):
        super().do_q()
        self.device.clips.append(self.device.clip)

    def do_Q(self):
        super().do_Q()
        self.device.clip = self.device.clips.pop() if self.device.clips else None

    def do_W(self):
        self.device.clip = intersection(self.device.clip, rectangle(self.curpath, self.ctm))

    do_W_a = do_W

    def do_gs(self, name):
        # Extended state can change opacity or mask text. Only plain Calc paint is provable.
        raise QAProblem("Unsupported PDF graphics state prevents verification of preview visibility.")

    def do_Do(self, xobjid_arg):
        raise QAProblem("Unsupported PDF objects prevent verification of preview visibility.")


class PreviewPaint(PDFTextDevice):
    def __init__(self, resource_manager, bounds, *, marker_colors=None, cell_bounds=None):
        super().__init__(resource_manager)
        self.clip = bounds
        self.clips = []
        self.marker_colors = marker_colors
        self.marker_boxes = defaultdict(set)
        self.cell_refs = {LTComponent(box): ref for ref, box in (cell_bounds or {}).items()}
        self.regions = Plane(bounds)
        self.regions.extend(self.cell_refs)
        self.text = defaultdict(list)
        self.painted = False

    def paint_path(self, gstate, stroke, fill, evenodd, path):
        if self.marker_colors is None or not fill or gstate.ncs.name != "DeviceRGB":
            return
        red, green, blue = (round(channel * 255) for channel in gstate.ncolor)
        ref = self.marker_colors.get((red << 16) | (green << 8) | blue)
        if ref is not None and (box := intersection(self.clip, rectangle(path, self.ctm))) is not None:
            self.marker_boxes[ref].add(box)

    def render_string(self, textstate, seq, ncs, graphicstate):
        if self.marker_colors is None:
            # Invisible text does not witness coverage. Text-based clipping is unsupported.
            if textstate.render not in {0, 1, 2, 3}:
                raise QAProblem("Unsupported PDF text clipping prevents verification of preview visibility.")
            self.painted = textstate.render != 3
            super().render_string(textstate, seq, ncs, graphicstate)

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        width = font.char_width(cid)
        try:
            text = font.to_unichr(cid)
        except PDFUnicodeNotDefined:
            text = "\ufffd"
        if self.painted and text.strip():
            glyph = LTChar(matrix, font, fontsize, scaling, rise, text, width, font.char_disp(cid), ncs, graphicstate)
            if contains(self.clip, glyph.bbox):
                cells = [cell for cell in self.regions.find(glyph.bbox) if contains(cell.bbox, glyph.bbox)]
                if len(cells) == 1:
                    self.text[self.cell_refs[cells[0]]].append(text)
        return width * fontsize * scaling


def page_bounds(page):
    if page.rotate or page.mediabox[:2] != (0, 0) or not all(
        isfinite(size) and 0 < size <= MAX_PAGE_POINTS for size in page.mediabox[2:]
    ):
        raise QAProblem("The rendered worksheet has unsupported preview page dimensions.")
    bounds = intersection(page.mediabox, page.cropbox)
    if bounds is None:
        raise QAProblem("The rendered worksheet has no visible preview area.")
    return bounds


def verify_preview(book, pdf, measured, colors, witnesses):
    try:
        pages = list(PDFPage.get_pages(BytesIO(pdf)))
        measurements = list(PDFPage.get_pages(BytesIO(measured)))
        if len(pages) != len(book.sheets) or len(measurements) != len(pages):
            raise QAProblem("Preview page count does not cover every worksheet.")
        report, incomplete = [], []
        resource_manager = PDFResourceManager()
        measurement_resources = PDFResourceManager()
        for page_no, (sheet, page, layout) in enumerate(zip(book.sheets.values(), pages, measurements), 1):
            bounds = page_bounds(page)
            if page.mediabox != layout.mediabox or bounds != page_bounds(layout):
                raise QAProblem("The rendered worksheet and its measured regions use different page bounds.", [{"sheet": sheet.name}])
            marker_paint = PreviewPaint(measurement_resources, bounds, marker_colors=colors[sheet.name])
            PreviewInterpreter(measurement_resources, marker_paint).process_page(layout)
            regions = {ref: next(iter(boxes)) for ref, boxes in marker_paint.marker_boxes.items() if len(boxes) == 1}
            paint = PreviewPaint(resource_manager, bounds, cell_bounds=regions)
            PreviewInterpreter(resource_manager, paint).process_page(page)
            expected = witnesses[sheet.name]
            incomplete.extend(
                location(sheet.name, ref) for ref, value in expected.items()
                if text_key("".join(paint.text.get(ref, ()))) != value
            )
            report.append({
                "sheet": sheet.name, "visibility": sheet.state, "page": page_no,
                "range": populated_range(list(expected)), "populated_cells": len(expected),
                "note_cells": [item["coordinate"] for item in sheet.notes],
                "hidden_rows_and_columns_expanded": True,
            })
    except (PSException, ValueError, TypeError, AttributeError, AssertionError, IndexError, OverflowError) as error:
        raise QAProblem("The worksheet preview could not be reopened.") from error
    if incomplete:
        raise QAProblem("The preview omits or clips one or more populated cells.", incomplete[:200])
    return report
