"""Read SpreadsheetML evidence without interpreting formulas or following links."""

import json
import re
from bisect import bisect_left, bisect_right
from heapq import heappop, heappush
from io import BytesIO
from pathlib import PurePosixPath
from typing import NamedTuple
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.ElementTree import fromstring, iterparse
from defusedxml.common import DefusedXmlException
from app.outcomes import FailureKind, UseCaseError

from app.features.extraction.fields import identify_fields
from app.features.tasks.upload import (
    DOCUMENT_RELATIONSHIP_NAMESPACES, MAX_EXPANDED_BYTES, MAX_UPLOAD_BYTES,
    SHEET_TYPES, internal_target, relationships, validate_workbook,
)

EXTRACTOR_VERSION = "xlsx-evidence-v2"
MAX_SHEETS = 100
MAX_CELLS = 50_000
MAX_CONTEXT_RECORDS = 100_000
MAX_CONTEXT_PART_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
CELL_REFERENCE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
THREAD_NS = "{http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments}"

# Locale-dependent built-in formats keep their IDs even if no code is listed here.
BUILTIN_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yy h:mm", 37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}


class ExtractionProblem(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail


class Budget:
    def __init__(self):
        self.cells = 0
        self.context = 0

    def cell(self):
        self.cells += 1
        if self.cells > MAX_CELLS:
            raise ExtractionProblem("cell_limit", "Workbook exceeds the extraction cell limit")

    def record(self):
        self.context += 1
        if self.context > MAX_CONTEXT_RECORDS:
            raise ExtractionProblem("context_limit", "Workbook exceeds the extraction structure limit")


def namespace(tag: str) -> str:
    return tag.split("}", 1)[0] + "}" if tag.startswith("{") else ""


def context_xml(archive: ZipFile, part: str):
    if archive.getinfo(part).file_size > MAX_CONTEXT_PART_BYTES:
        raise ExtractionProblem("context_limit", "Workbook context part exceeds the extraction size limit")
    return fromstring(archive.read(part), forbid_dtd=True)


def rich_text(element, ns: str) -> str:
    return "".join(
        text.text or ""
        for child in element
        for text in ([child] if child.tag == f"{ns}t" else (
            child.findall(f"{ns}t") if child.tag == f"{ns}r" else []
        ))
    )


def root_items(archive: ZipFile, part: str):
    """Yield direct XML children while freeing each subtree after use."""
    with archive.open(part) as stream:
        stack = []
        for event, element in iterparse(stream, events=("start", "end"), forbid_dtd=True):
            if event == "start":
                stack.append(element)
                if len(stack) > 64:
                    raise ExtractionProblem("context_limit", "Workbook XML nesting exceeds the extraction limit")
                continue
            if len(stack) == 2:
                yield element
                stack[0].remove(element)
            stack.pop()


def find_part(rels, directory: str, kind: str) -> str | None:
    matches = [rel for rel in rels if rel.get("Type", "").rsplit("/", 1)[-1] == kind]
    if len(matches) > 1:
        raise BadZipFile
    return internal_target(matches[0], directory) if matches else None


def shared_strings(archive: ZipFile, part: str | None, budget: Budget) -> list[str]:
    strings = []
    if part:
        for element in root_items(archive, part):
            ns = namespace(element.tag)
            if element.tag == f"{ns}si":
                budget.record()
                strings.append(rich_text(element, ns))
    return strings


def style_formats(archive: ZipFile, part: str | None) -> tuple[list[int], dict[int, str]]:
    codes = BUILTIN_FORMATS.copy()
    if not part:
        return [0], codes
    styles = context_xml(archive, part)
    ns = namespace(styles.tag)
    codes.update({
        int(item.attrib["numFmtId"]): item.attrib["formatCode"]
        for item in styles.findall(f"{ns}numFmts/{ns}numFmt")
    })
    identifiers = [
        int(style.get("numFmtId", "0"))
        for style in styles.findall(f"{ns}cellXfs/{ns}xf")
    ]
    return identifiers or [0], codes


def coordinate_parts(coordinate: str) -> tuple[int, int]:
    match = CELL_REFERENCE.fullmatch(coordinate)
    if match is None:
        raise BadZipFile
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if column > 16_384 or row > 1_048_576:
        raise BadZipFile
    return row, column


def coordinate_name(row: int, column: int) -> str:
    letters = ""
    while column:
        column, index = divmod(column - 1, 26)
        letters = chr(ord("A") + index) + letters
    return f"{letters}{row}"


def formula_cache_status(kind: str, value: str | None) -> str:
    # A present empty string result is distinct from a missing result.
    return "saved_unverified" if value is not None and (value != "" or kind == "str") else "missing"


def cell_evidence(element, ns, coordinate, column, strings, formats) -> dict:
    style_ids, format_codes = formats
    kind = element.get("t", "n")
    value = element.findtext(f"{ns}v")
    inline = element.find(f"{ns}is")
    formula = element.find(f"{ns}f")
    style_id = int(element.get("s", "0"))
    if style_id < 0 or style_id >= len(style_ids):
        raise BadZipFile
    fmt_id = style_ids[style_id]
    display_text = None
    if kind == "s" and value not in (None, ""):
        index = int(value)
        if index < 0 or index >= len(strings):
            raise BadZipFile
        display_text = strings[index]
    elif inline is not None:
        display_text = rich_text(inline, ns)
    elif kind == "str" and value is not None:
        display_text = value
    return {
        "coordinate": coordinate,
        "column": column,
        "cell_type": kind,
        "raw_value": value,
        "text": display_text,
        "number_format": {"id": fmt_id, "code": format_codes.get(fmt_id)},
        "formula": {
            "text": formula.text,
            "attributes": dict(formula.attrib),
            "cache_status": formula_cache_status(kind, value),
        } if formula is not None else None,
    }


class ArrayRange(NamedTuple):
    first_row: int
    last_row: int
    first_column: int
    last_column: int
    anchor: str
    reference: str


def array_range(coordinate: str, attributes: dict) -> ArrayRange:
    reference = attributes.get("ref", "")
    first, separator, last = reference.partition(":")
    first_row, first_column = coordinate_parts(first)
    last_row, last_column = coordinate_parts(last if separator else first)
    if first != coordinate or last_row < first_row or last_column < first_column:
        raise BadZipFile
    return ArrayRange(first_row, last_row, first_column, last_column, coordinate, reference)


def mark_array_results(rows: list[dict], arrays: list[ArrayRange]) -> None:
    if not arrays:
        return

    # Track active ranges by starting column and expire them by ending row.
    # Visit only stored cells, even when a declared range covers a large sparse area.
    starts = iter(arrays)
    pending = next(starts, None)
    active: list[ArrayRange] = []
    endings: list[tuple[int, int]] = []

    def column_key(array: ArrayRange) -> int:
        return array.first_column

    for row in rows:
        number = row["row"]
        while endings and endings[0][0] < number:
            _, column = heappop(endings)
            active.pop(bisect_left(active, column, key=column_key))
        while pending is not None and pending.first_row == number:
            index = bisect_left(active, pending.first_column, key=column_key)
            if (
                index > 0 and active[index - 1].last_column >= pending.first_column
                or index < len(active) and active[index].first_column <= pending.last_column
            ):
                raise BadZipFile
            active.insert(index, pending)
            heappush(endings, (pending.last_row, pending.first_column))
            pending = next(starts, None)
        if not active:
            continue
        for cell in row["cells"]:
            column = cell["column"]
            index = bisect_right(active, column, key=column_key) - 1
            if index < 0 or active[index].last_column < column:
                continue
            array = active[index]
            if cell["coordinate"] == array.anchor:
                cell["formula"]["anchor"] = array.anchor
                continue
            if cell["formula"] is not None:
                raise BadZipFile
            cell["formula"] = {
                "text": None,
                "attributes": {},
                "type": "array",
                "range": array.reference,
                "anchor": array.anchor,
                "cache_status": formula_cache_status(cell["cell_type"], cell["raw_value"]),
            }


def worksheet_cells(archive: ZipFile, part: str, ns: str, strings, formats, budget: Budget) -> dict:
    rows, columns, merges = [], [], []
    stack = []
    active_cell = None
    row_number = column_number = 0
    row = None
    shared_anchors = {}
    shared_followers = []
    arrays = []

    with archive.open(part) as stream:
        for event, element in iterparse(stream, events=("start", "end"), forbid_dtd=True):
            if event == "start":
                stack.append(element)
                if len(stack) > 64:
                    raise ExtractionProblem("context_limit", "Workbook XML nesting exceeds the extraction limit")
                parent_tag = stack[-2].tag if len(stack) > 1 else None
                if element.tag == f"{ns}row" and parent_tag == f"{ns}sheetData":
                    budget.record()
                    next_row = int(element.get("r", row_number + 1))
                    if not row_number < next_row <= 1_048_576:
                        raise BadZipFile
                    row_number = next_row
                    column_number = 0
                    row = {"row": row_number, "hidden": element.get("hidden") in {"1", "true"}, "cells": []}
                if element.tag == f"{ns}c" and parent_tag == f"{ns}row" and row is not None:
                    budget.cell()
                    active_cell = element
                continue

            if element is active_cell:
                coordinate = element.get("r") or coordinate_name(row_number, column_number + 1)
                declared_row, declared_column = coordinate_parts(coordinate)
                if declared_row != row_number or declared_column <= column_number:
                    raise BadZipFile
                column_number = declared_column
                cell = cell_evidence(element, ns, coordinate, column_number, strings, formats)
                row["cells"].append(cell)
                if (formula := cell["formula"]) and formula["attributes"].get("t") == "shared":
                    identifier = formula["attributes"].get("si")
                    if identifier is not None:
                        if formula["text"] is not None:
                            shared_anchors[identifier] = coordinate
                        else:
                            shared_followers.append((identifier, formula))
                if formula and formula["attributes"].get("t") == "array":
                    arrays.append(array_range(coordinate, formula["attributes"]))
                active_cell = None
            elif active_cell is None:
                if element.tag == f"{ns}row" and row is not None:
                    rows.append(row)
                    row = None
                elif element.tag == f"{ns}col":
                    budget.record()
                    columns.append({
                        "first": int(element.attrib["min"]), "last": int(element.attrib["max"]),
                        "hidden": element.get("hidden") in {"1", "true"},
                    })
                elif element.tag == f"{ns}mergeCell":
                    budget.record()
                    merges.append(element.attrib["ref"])

            if active_cell is None and len(stack) > 1:
                stack[-2].remove(element)
            stack.pop()

    for identifier, formula in shared_followers:
        formula["anchor"] = shared_anchors.get(identifier)
    mark_array_results(rows, arrays)
    return {"rows": rows, "columns": columns, "merged_ranges": merges}


def sheet_comments(archive: ZipFile, part: str, budget: Budget) -> list[dict]:
    path = PurePosixPath(part)
    rels_path = str(path.parent / "_rels" / (path.name + ".rels"))
    if rels_path not in archive.namelist():
        return []
    rels = relationships(archive, rels_path)
    comments = []

    if location := find_part(rels, str(path.parent), "comments"):
        root = context_xml(archive, location)
        ns = namespace(root.tag)
        authors = [author.text or "" for author in root.findall(f"{ns}authors/{ns}author")]
        for note in root.findall(f"{ns}commentList/{ns}comment"):
            budget.record()
            ref = note.attrib["ref"]
            coordinate_parts(ref)
            author_index = int(note.attrib["authorId"])
            if not 0 <= author_index < len(authors):
                raise BadZipFile
            content = note.find(f"{ns}text")
            comments.append({
                "coordinate": ref, "kind": "note", "author": authors[author_index],
                "text": rich_text(content, ns) if content is not None else "",
            })

    if location := find_part(rels, str(path.parent), "threadedComment"):
        root = context_xml(archive, location)
        for comment in root.findall(f"{THREAD_NS}threadedComment"):
            budget.record()
            ref = comment.attrib["ref"]
            coordinate_parts(ref)
            comments.append({
                "coordinate": ref, "kind": "threaded_comment",
                "text": comment.findtext(f"{THREAD_NS}text") or "",
                "attributes": dict(comment.attrib),
            })
    return comments


def extract_workbook(data: bytes) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtractionProblem("upload_limit", "Workbook exceeds the extraction upload size limit")
    try:
        validate_workbook(data)
    except UseCaseError as error:
        if error.kind == FailureKind.TOO_LARGE:
            raise ExtractionProblem("expanded_limit", "Workbook exceeds the 64 MiB expanded limit") from None
        raise ExtractionProblem("invalid_workbook", "Stored workbook package is invalid or unsupported") from None

    try:
        with ZipFile(BytesIO(data)) as archive:
            # Reuse the upload package policy and keep its uncompressed byte limit explicit.
            if sum(part.file_size for part in archive.infolist()) > MAX_EXPANDED_BYTES:
                raise ExtractionProblem("expanded_limit", "Workbook exceeds the 64 MiB expanded limit")
            workbook = context_xml(archive, "xl/workbook.xml")
            ns = namespace(workbook.tag)
            entries = workbook.findall(f"{ns}sheets/{ns}sheet")
            if len(entries) > MAX_SHEETS:
                raise ExtractionProblem("sheet_limit", "Workbook exceeds the extraction sheet limit")
            if len({sheet.attrib["name"].casefold() for sheet in entries}) != len(entries):
                raise BadZipFile
            rels = relationships(archive, "xl/_rels/workbook.xml.rels")
            by_id = {rel.attrib["Id"]: rel for rel in rels}
            budget = Budget()
            strings = shared_strings(archive, find_part(rels, "xl", "sharedStrings"), budget)
            formats = style_formats(archive, find_part(rels, "xl", "styles"))
            sheets = []

            for position, sheet in enumerate(entries, start=1):
                rel_id = next(
                    sheet.attrib[f"{{{uri}}}id"] for uri in DOCUMENT_RELATIONSHIP_NAMESPACES
                    if f"{{{uri}}}id" in sheet.attrib
                )
                rel = by_id[rel_id]
                part = internal_target(rel, "xl")
                sheets.append({
                    "name": sheet.attrib["name"], "position": position,
                    "visibility": sheet.get("state", "visible"), "type": SHEET_TYPES[rel.attrib["Type"]],
                    "part": part, **worksheet_cells(archive, part, ns, strings, formats, budget),
                    "comments": sheet_comments(archive, part, budget),
                })
            options = workbook.find(f"{ns}workbookPr")
            calc = workbook.find(f"{ns}calcPr")
            raw = {
                "date_system": "1904" if options is not None and options.get("date1904") in {"1", "true"} else "1900",
                "calculation_settings": dict(calc.attrib) if calc is not None else {},
                "sheets": sheets,
            }
    except (BadZipFile, ParseError, DefusedXmlException, LookupError, ValueError, StopIteration):
        raise ExtractionProblem("invalid_workbook", "Stored workbook evidence could not be read") from None

    result = {"raw": raw, "identified": identify_fields(sheets)}
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(result):
        size += len(chunk.encode())
        if size > MAX_RESULT_BYTES:
            raise ExtractionProblem("result_limit", "Workbook exceeds the extraction result size limit")
    return result
