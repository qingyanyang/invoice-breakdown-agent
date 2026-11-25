"""Inspect exact OOXML bytes and import only independently recalculated caches."""

from dataclasses import dataclass
from io import BytesIO
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from fastapi import HTTPException
from lxml import etree as xml

from app.extraction_reader import (
    Budget, ExtractionProblem, coordinate_name, coordinate_parts, find_part, shared_strings, sheet_comments, style_formats, worksheet_cells,
)
from app.workbook_layout import ADJUSTMENT, SUMMARY
from app.workbook_source import R, S, WORKBOOK, WORKBOOK_RELS, MAX_OUTPUT_BYTES, WorkbookProblem, parse, serialize
from app.workbook_upload import MAX_EXPANDED_BYTES, internal_target, relationships, validate_workbook

REVIEW_TEXT = "Generated for review · Awaiting QA"
CACHED_TYPES = {"n", "str", "s", "inlineStr", "b", "e", "d"}


class QAProblem(Exception):
    def __init__(self, detail: str, locations: list[dict] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.locations = locations or []


def location(sheet: str, cell: str) -> dict:
    return {"sheet": sheet, "cell": cell}


@dataclass
class QASheet:
    name: str
    state: str
    part: str
    root: object
    cells: dict[str, dict]
    nodes: dict[str, object]
    notes: list[dict]

    def formulas(self):
        return {ref: node for ref, node in self.nodes.items() if node.find(S + "f") is not None}


class QABook:
    def __init__(self, data: bytes, *, engine_output: bool = False):
        if len(data) > MAX_OUTPUT_BYTES:
            raise QAProblem("Workbook exceeds the QA size limit.")
        try:
            validate_workbook(data)
            with ZipFile(BytesIO(data)) as source:
                self.parts = {name: source.read(name) for name in source.namelist()}
                if not engine_output:
                    self.reject_external_relationships()
                self.root = parse(self.parts[WORKBOOK])
                relations = relationships(source, WORKBOOK_RELS)
                by_id = {item.get("Id"): item for item in relations}
                strings = shared_strings(source, find_part(relations, "xl", "sharedStrings"), Budget())
                self.style_part = find_part(relations, "xl", "styles")
                formats = style_formats(source, self.style_part)
                self.sheets = {}
                self.date_1904 = self.root.find(S + "workbookPr") is not None and (
                    self.root.find(S + "workbookPr").get("date1904") in {"1", "true"}
                )
                for item in self.root.find(S + "sheets"):
                    name = item.get("name")
                    part = internal_target(by_id[item.get(R + "id")], "xl")
                    root = parse(self.parts[part])
                    if root.tag != S + "worksheet" or name in self.sheets:
                        raise QAProblem("QA requires a distinct worksheet for each recorded sheet.")
                    rows = worksheet_cells(source, part, S, strings, formats, Budget())["rows"]
                    cells = {cell["coordinate"]: cell for row in rows for cell in row["cells"]}
                    nodes = {
                        cell["coordinate"]: node
                        for row, xml_row in zip(rows, root.findall(f"{S}sheetData/{S}row"), strict=True)
                        for cell, node in zip(row["cells"], xml_row.findall(S + "c"), strict=True)
                    }
                    notes = sheet_comments(source, part, Budget())
                    self.sheets[name] = QASheet(name, item.get("state", "visible"), part, root, cells, nodes, notes)
                if len(self.sheets) > 103 or sum(len(sheet.cells) for sheet in self.sheets.values()) > 200_000:
                    raise QAProblem("Workbook exceeds the QA worksheet or cell limit.")
        except (BadZipFile, ExtractionProblem, WorkbookProblem, HTTPException, ValueError, LookupError,
                TypeError, xml.LxmlError) as error:
            raise QAProblem("Workbook could not be reopened for QA.") from error

    def reject_external_relationships(self):
        forbidden = {"externalLink", "oleObject", "queryTable", "connections", "vbaProject"}
        for name, data in self.parts.items():
            if name.endswith(".rels") and any(
                node.get("TargetMode", "").lower() == "external"
                or (node.get("Type") or "").rsplit("/", 1)[-1] in forbidden
                for node in parse(data)
            ):
                raise QAProblem("Live external workbook relationships are prohibited during QA.")

    def dump(self, changed_sheets, *, workbook_changed=False) -> bytes:
        parts = self.parts | {sheet.part: serialize(sheet.root) for sheet in changed_sheets}
        if workbook_changed:
            parts[WORKBOOK] = serialize(self.root)
        if sum(map(len, parts.values())) > MAX_EXPANDED_BYTES:
            raise QAProblem("The QA workbook exceeds the expanded size limit.")
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for name, payload in sorted(parts.items()):
                archive.writestr(name, payload)
        output = buffer.getvalue()
        if len(output) > MAX_OUTPUT_BYTES:
            raise QAProblem("The QA workbook exceeds the stored size limit.")
        return output


def remove_values(node):
    for value in node.findall(S + "v"):
        node.remove(value)
    for inline in node.findall(S + "is"):
        node.remove(inline)
    node.attrib.pop("t", None)


def strip_formula_caches(data: bytes) -> tuple[bytes, int]:
    book = QABook(data)
    changed = []
    count = 0
    for sheet in book.sheets.values():
        if formulas := sheet.formulas():
            changed.append(sheet)
            count += len(formulas)
            for node in formulas.values():
                remove_values(node)
    settings = book.root.find(S + "calcPr")
    if settings is None:
        settings = xml.SubElement(book.root, S + "calcPr")
    settings.attrib.update({"fullCalcOnLoad": "1", "forceFullCalc": "1", "calcMode": "auto"})
    properties = book.root.find(S + "workbookPr")
    if properties is not None:
        properties.set("updateLinks", "never")
    return book.dump(changed, workbook_changed=True), count


def formula_text(node) -> str:
    return node.findtext(S + "f") or ""


def write_cache(node, observed: dict):
    kind, value = observed["cell_type"], observed["raw_value"]
    if kind in {"s", "inlineStr"}:
        kind, value = "str", observed["text"]
    if kind not in CACHED_TYPES or value is None or (value == "" and kind != "str"):
        return False
    remove_values(node)
    if kind != "n":
        node.set("t", kind)
    xml.SubElement(node, S + "v").text = value
    return True


def remove_review_banner(sheet: QASheet) -> bool:
    if sheet.name not in {SUMMARY, ADJUSTMENT} or sheet.cells.get("A2", {}).get("text") != REVIEW_TEXT:
        return False
    node = sheet.nodes["A2"]
    node.getparent().remove(node)
    return True


def candidate_from_calculation(original: bytes, calculated: bytes) -> bytes:
    book, engine = QABook(original), QABook(calculated, engine_output=True)
    if list(book.sheets) != list(engine.sheets):
        raise QAProblem("The calculation engine returned a different worksheet inventory.")
    errors = []
    changed = []
    for name, sheet in book.sheets.items():
        formulas, calculated_formulas = sheet.formulas(), engine.sheets[name].formulas()
        if set(formulas) != set(calculated_formulas):
            errors.extend(location(name, ref) for ref in sorted(set(formulas) ^ set(calculated_formulas)))
        for ref, node in formulas.items():
            returned = calculated_formulas.get(ref)
            if returned is None:
                continue
            # Formula text is conservative by design. A changed expression requires review.
            if formula_text(returned) != formula_text(node) or not write_cache(node, engine.sheets[name].cells[ref]):
                errors.append(location(name, ref))
        if remove_review_banner(sheet) or formulas:
            changed.append(sheet)
    if errors:
        raise QAProblem("The engine did not return fresh results for the original formulas.", errors[:200])
    candidate = book.dump(changed)
    verify_cache_only_changes(original, candidate)
    return candidate


def verify_cache_only_changes(original: bytes, candidate: bytes):
    before, after = QABook(original), QABook(candidate)
    if before.parts.keys() != after.parts.keys() or list(before.sheets) != list(after.sheets):
        raise QAProblem("The delivery candidate has a different package inventory.")
    sheet_parts = {sheet.part for sheet in before.sheets.values()}
    if any(before.parts[name] != after.parts[name] for name in before.parts.keys() - sheet_parts):
        raise QAProblem("Recalculation changed workbook styles, relationships or workbook metadata.")
    changed = []
    for name, sheet in before.sheets.items():
        remove_review_banner(sheet)
        compared = after.sheets[name]
        for item in (sheet, compared):
            for node in item.formulas().values():
                remove_values(node)
        if xml.tostring(sheet.root, method="c14n") != xml.tostring(compared.root, method="c14n"):
            changed.append({"sheet": name})
    if changed:
        raise QAProblem("Recalculation changed original worksheet content, formulas or layout.", changed)


def render_copy(data: bytes) -> bytes:
    book = QABook(data)
    for entry in book.root.find(S + "sheets"):
        entry.attrib.pop("state", None)
    for sheet in book.sheets.values():
        for node in sheet.root.findall(f"{S}sheetData/{S}row") + sheet.root.findall(f"{S}cols/{S}col"):
            node.attrib.pop("hidden", None)
    return book.dump(book.sheets.values(), workbook_changed=True)


def populated_range(refs: list[str]) -> str | None:
    if not refs:
        return None
    coords = [coordinate_parts(ref) for ref in refs]
    start = coordinate_name(min(row for row, _ in coords), min(col for _, col in coords))
    end = coordinate_name(max(row for row, _ in coords), max(col for _, col in coords))
    return start if start == end else f"{start}:{end}"
