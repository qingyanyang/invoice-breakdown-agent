"""Copy source worksheet parts with conservative formula and relationship handling."""

import re
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from app.outcomes import UseCaseError
from lxml import etree as xml

from app.features.extraction.reader import (
    Budget, coordinate_parts, formula_cache_status, shared_strings, sheet_comments, style_formats, worksheet_cells,
)
from app.features.reconciliation.evidence import additive_references, markup_recipe, quantity_rate_recipe
from app.features.tasks.upload import (
    DOCUMENT_RELATIONSHIP_NAMESPACES, MAX_EXPANDED_BYTES, PACKAGE_RELATIONSHIPS, internal_target, validate_workbook,
)

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
S = f"{{{MAIN}}}"
R = f"{{{DOCUMENT_RELATIONSHIP_NAMESPACES[0]}}}"
REL = PACKAGE_RELATIONSHIPS
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
WORKBOOK = "xl/workbook.xml"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
PACKAGE_RELS = "_rels/.rels"
CONTENT_TYPES = "[Content_Types].xml"
STRINGS = "sharedStrings"
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

WORKBOOK_ELEMENTS = {
    "fileVersion", "fileSharing", "workbookPr", "workbookProtection", "bookViews", "sheets", "definedNames",
    "calcPr", "externalReferences",
}
SHEET_ELEMENTS = {
    "sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData", "sheetProtection", "autoFilter",
    "sortState", "mergeCells", "phoneticPr", "printOptions", "pageMargins", "pageSetup", "headerFooter",
    "rowBreaks", "colBreaks", "ignoredErrors", "legacyDrawing", "hyperlinks", "conditionalFormatting",
}
NAME_UNSAFE = re.compile(r"[\[\]:*?/\\\x00-\x1f]")
CF_RULES = {"cellIs", "expression", "colorScale", "dataBar", "iconSet", "top10", "uniqueValues", "duplicateValues", "aboveAverage"}
CF_ELEMENTS = {"cfRule", "formula", "colorScale", "dataBar", "iconSet", "cfvo", "color"}
CF_REF = r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}"
CF_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?"
CF_OPERAND = rf'(?:{CF_REF}(?::{CF_REF})?|{CF_NUMBER}|TRUE|FALSE|"(?:[^"]|"")*")'
CF_RANGE = re.compile(rf"({CF_REF})(?::({CF_REF}))?", re.I)
LOCAL_CF_FORMULA = re.compile(rf"({CF_OPERAND})(?:\s*(?:<>|>=|<=|>|<|=)\s*({CF_OPERAND}))?", re.I)


class WorkbookProblem(Exception):
    def __init__(self, code: str, detail: str, *, sheet: str | None = None, feature: str | None = None):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail
        self.sheet = sheet
        self.feature = feature

    def report(self) -> dict:
        return {"code": self.code, "detail": self.detail, "sheet": self.sheet, "feature": self.feature}


def unsupported(sheet: str | None, feature: str) -> WorkbookProblem:
    return WorkbookProblem(
        "unsupported_source_feature", f"Preservation of {feature} is unsupported in a review workbook.",
        sheet=sheet, feature=feature,
    )


def parse(data: bytes):
    root = xml.fromstring(data, parser=xml.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if root.getroottree().docinfo.doctype:
        raise WorkbookProblem("unsafe_source_xml", "Source XML contains an unsupported document type declaration.")
    return root


def serialize(element) -> bytes:
    return xml.tostring(element, encoding="UTF-8", xml_declaration=True, standalone=True)


def relationship_kind(node) -> str:
    return (node.get("Type") or "").rsplit("/", 1)[-1]


def relationship_part(part: str) -> str:
    path = PurePosixPath(part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def available_sheet_name(name: str, reserved: set[str]) -> str:
    base = NAME_UNSAFE.sub("-", name).strip(" '") or "Source Data"
    base = base[:31].rstrip("'")
    candidate = base
    suffix = 2
    while candidate.casefold() in reserved or candidate.casefold() == "history":
        tail = f" {suffix}"
        candidate = base[:31 - len(tail)].rstrip("' ") + tail
        suffix += 1
    reserved.add(candidate.casefold())
    return candidate


def formula_dependencies(sheet: str, text: str) -> set[str] | None:
    # Cross-sheet, defined-name, and external dependencies are kept as evidence.
    if "!" in text or "[" in text or "]" in text:
        return None
    if refs := additive_references(text, sheet):
        return {ref.cell for ref, _ in refs}
    if recipe := markup_recipe(text, sheet):
        base, markup = recipe
        return {ref.cell for ref in [*base, markup]}
    if refs := quantity_rate_recipe(text, sheet):
        return {ref.cell for ref in refs}
    return None


def local_conditional_formula(sheet: str, expression: str) -> bool:
    if comparison := LOCAL_CF_FORMULA.fullmatch(expression):
        refs = []
        for operand in comparison.groups():
            if operand is not None and (cell_range := CF_RANGE.fullmatch(operand)):
                refs.extend(cell for cell in cell_range.groups() if cell is not None)
    else:
        parsed_refs = formula_dependencies(sheet, expression)
        if parsed_refs is None:
            return False
        refs = parsed_refs
    try:
        for ref in refs:
            coordinate_parts(ref.upper().replace("$", ""))
    except BadZipFile:
        return False
    return True


def active_formulas(sheet: str, cells: dict[str, dict]) -> set[str]:
    candidates = {}
    dependents = defaultdict(list)
    pending = {}
    for coordinate, item in cells.items():
        formula = item["formula"]
        if formula is None or formula["attributes"] or not formula["text"]:
            continue
        refs = formula_dependencies(sheet, formula["text"])
        if not refs or any(
            ref not in cells or (
                cells[ref]["formula"] is None and cells[ref]["raw_value"] is None and cells[ref]["text"] is None
            )
            for ref in refs
        ):
            continue
        dependencies = {ref for ref in refs if cells[ref]["formula"] is not None}
        candidates[coordinate] = dependencies
        for ref in dependencies:
            dependents[ref].append(coordinate)
        pending[coordinate] = len(dependencies)

    queue = deque(coordinate for coordinate, refs in candidates.items() if not refs)
    safe = set()
    while queue:
        coordinate = queue.popleft()
        safe.add(coordinate)
        for dependent in dependents[coordinate]:
            pending[dependent] -= 1
            if not pending[dependent]:
                queue.append(dependent)
    return safe


@dataclass
class SourceSheet:
    before: dict
    output_name: str
    expected: dict
    formula_count: int
    snapshot_count: int


class SourcePackage:
    def __init__(self, original: ZipFile, evidence: dict, reserved: set[str]):
        self.original = original
        self.parts = {}
        self.formulas = []
        self.links = []
        self.names = []
        self.sheets = []
        self.workbook = self.read_xml(WORKBOOK)
        if self.workbook.tag != S + "workbook":
            raise unsupported(None, "Strict SpreadsheetML workbook")
        self.workbook_rels = self.read_xml(WORKBOOK_RELS)
        self.package_rels = self.read_xml(PACKAGE_RELS)
        self.content_types = self.read_xml(CONTENT_TYPES)
        self.entries = self.workbook.find(S + "sheets")
        self.style_part = None
        self.strings_part = None
        self._check_elements(self.workbook, WORKBOOK_ELEMENTS, None)
        self._copy_root_properties()
        self._copy_sheets(evidence["raw"]["sheets"], reserved)
        self._copy_workbook_resources()
        self._set_active_summary()

    def read_xml(self, name):
        return parse(self.original.read(name))

    @staticmethod
    def _check_elements(root, supported, sheet):
        for child in root:
            if not isinstance(child.tag, str):
                continue
            name = xml.QName(child).localname
            if child.tag != S + name or name not in supported:
                raise unsupported(sheet, name)

    def _copy_leaf(self, relation, owner: str, sheet: str | None = None) -> str:
        part = internal_target(relation, owner)
        if part not in self.original.namelist() or relationship_part(part) in self.original.namelist():
            raise unsupported(sheet, relationship_kind(relation))
        self.parts[part] = self.original.read(part)
        return part

    def _copy_root_properties(self):
        for relation in tuple(self.package_rels):
            kind = relationship_kind(relation)
            if kind == "officeDocument":
                continue
            if kind == "core-properties":
                self._copy_leaf(relation, "")
            elif kind in {"extended-properties", "custom-properties", "thumbnail"}:
                # Workbook-level document properties are separate from worksheet evidence.
                self.package_rels.remove(relation)
            else:
                raise unsupported(None, kind)

    def _copy_workbook_resources(self):
        for relation in tuple(self.workbook_rels):
            kind = relationship_kind(relation)
            if kind == "worksheet":
                continue
            if kind in {"calcChain", "externalLink"}:
                self.workbook_rels.remove(relation)
                continue
            if kind not in {"styles", STRINGS, "theme", "person"}:
                raise unsupported(None, kind)
            part = self._copy_leaf(relation, "xl")
            if kind == "styles":
                self.style_part = part
            if kind == STRINGS:
                self.strings_part = part

        names = self.workbook.find(S + "definedNames")
        if names is not None:
            for node in names.findall(S + "definedName"):
                index = node.get("localSheetId")
                sheet = self.sheets[int(index)].before["name"] if index is not None else "Workbook"
                self.names.append((sheet, node.get("name", ""), node.text or ""))
            self.workbook.remove(names)
        refs = self.workbook.find(S + "externalReferences")
        if refs is not None:
            self.workbook.remove(refs)

    def _set_active_summary(self):
        views = self.workbook.find(S + "bookViews")
        if views is None:
            views = xml.Element(S + "bookViews")
            self.workbook.insert(self.workbook.index(self.entries), views)
            xml.SubElement(views, S + "workbookView")
        for view in views.findall(S + "workbookView"):
            view.set("activeTab", "0")
            view.set("firstSheet", "0")
        calc = self.workbook.find(S + "calcPr")
        if calc is None:
            calc = xml.SubElement(self.workbook, S + "calcPr")
        calc.set("calcMode", "auto")
        calc.set("fullCalcOnLoad", "1")
        # An original package may have requested refresh-on-open, including linked data.
        properties = self.workbook.find(S + "workbookPr")
        if properties is not None:
            properties.set("updateLinks", "never")

    @staticmethod
    def _check_conditional_formatting(root, sheet):
        for formatting in root.findall(S + "conditionalFormatting"):
            for node in formatting.iterdescendants():
                if not isinstance(node.tag, str):
                    continue
                kind = xml.QName(node).localname
                if node.tag != S + kind or kind not in CF_ELEMENTS:
                    raise unsupported(sheet, f"conditional formatting element {kind}")
                if kind == "cfRule" and node.get("type") not in CF_RULES:
                    raise unsupported(sheet, f"conditional formatting rule {node.get('type')}")
                if kind == "formula":
                    expression = (node.text or "").strip().removeprefix("=").strip()
                    if not local_conditional_formula(sheet, expression):
                        raise unsupported(sheet, "conditional formatting formula with unsupported dependencies")
                if kind == "cfvo":
                    value_type = node.get("type")
                    value = node.get("val")
                    if value_type not in {"min", "max", "num", "percent", "percentile"} or (
                        value is not None and not re.fullmatch(CF_NUMBER, value, re.I)
                    ):
                        raise unsupported(sheet, "conditional formatting threshold with unsupported dependencies")

    def _copy_sheets(self, sheets, reserved):
        if self.entries is None or len(self.entries) != len(sheets):
            raise WorkbookProblem("source_evidence_mismatch", "Source worksheet inventory differs from extraction.")
        relationships = {rel.get("Id"): rel for rel in self.workbook_rels}
        for entry, source in zip(self.entries, sheets):
            name = source["name"]
            if source["type"] != "worksheet":
                raise unsupported(name, source["type"])
            relation = relationships[entry.get(R + "id")]
            part = internal_target(relation, "xl")
            if part != source["part"] or entry.get("name") != name:
                raise WorkbookProblem("source_evidence_mismatch", "Worksheet does not match extraction.", sheet=name)
            output_name = available_sheet_name(name, reserved)
            entry.set("name", output_name)
            root = self.read_xml(part)
            self._check_elements(root, SHEET_ELEMENTS, name)
            self._check_conditional_formatting(root, name)
            for view in root.findall(f"{S}sheetViews/{S}sheetView"):
                view.attrib.pop("tabSelected", None)
            self._copy_worksheet_resources(root, part, name)
            expected, formula_count, snapshot_count = self._snapshot_formulas(root, source, output_name)
            self.parts[part] = serialize(root)
            self.sheets.append(SourceSheet(source, output_name, expected, formula_count, snapshot_count))

    def _copy_worksheet_resources(self, root, part, name):
        rel_path = relationship_part(part)
        links = root.find(S + "hyperlinks")
        relations = self.read_xml(rel_path) if rel_path in self.original.namelist() else None
        hyperlinks = {}
        if relations is not None:
            for relation in tuple(relations):
                kind = relationship_kind(relation)
                if kind == "hyperlink":
                    hyperlinks[relation.get("Id")] = relation.get("Target") or ""
                    relations.remove(relation)
                    continue
                if kind not in {"comments", "threadedComment", "vmlDrawing"}:
                    raise unsupported(name, kind)
                target = self._copy_leaf(relation, str(PurePosixPath(part).parent), name)
                if kind == "vmlDrawing":
                    for element in self.read_xml(target).iter():
                        if not isinstance(element.tag, str):
                            continue
                        local = xml.QName(element).localname
                        if (
                            local.lower() == "imagedata"
                            or local == "ClientData" and element.get("ObjectType") != "Note"
                            or any(xml.QName(attr).localname in {"src", "href", "relid"} for attr in element.attrib)
                        ):
                            raise unsupported(name, "VML content other than cell notes")
            if len(relations):
                self.parts[rel_path] = serialize(relations)
        if links is not None:
            for item in links.findall(S + "hyperlink"):
                target = hyperlinks.get(item.get(R + "id"), item.get("location") or "")
                self.links.append((name, item.get("ref") or "", target, item.get("display") or "", item.get("tooltip") or ""))
            root.remove(links)

    def _snapshot_formulas(self, root, source, output_name):
        expected = deepcopy(source)
        cells = {cell["coordinate"]: cell for row in expected["rows"] for cell in row["cells"]}
        safe = active_formulas(source["name"], cells)
        anchors = {ref: cell["formula"]["text"] for ref, cell in cells.items() if cell["formula"]}
        formula_count = len(anchors)
        for ref, cell in cells.items():
            formula = cell["formula"]
            if formula is None:
                continue
            anchor = formula.get("anchor")
            # Older extraction records still retain the raw blank-string result.
            formula["cache_status"] = formula_cache_status(cell["cell_type"], cell["raw_value"])
            self.formulas.append((
                source["name"], output_name, ref, anchor or "", formula["text"] or anchors.get(anchor) or "",
                cell["raw_value"] or "", formula["cache_status"], "Active internal formula" if ref in safe else "Source snapshot",
            ))
            if ref not in safe:
                cell["formula"] = None
                if cell["cell_type"] == "str" and cell["raw_value"] is not None:
                    cell["cell_type"] = "inlineStr"
                    cell["raw_value"] = None
                elif cell["raw_value"] == "":
                    cell["raw_value"] = None

        # Coordinates omitted by the source are implicit in row order.
        data = root.find(S + "sheetData")
        actual_rows = data.findall(S + "row") if data is not None else []
        if len(actual_rows) != len(expected["rows"]):
            raise WorkbookProblem("source_evidence_mismatch", "Source row count differs from extraction.", sheet=source["name"])
        for xml_row, row in zip(actual_rows, expected["rows"]):
            actual = xml_row.findall(S + "c")
            if len(actual) != len(row["cells"]):
                raise WorkbookProblem("source_evidence_mismatch", "Source cell count differs from extraction.", sheet=source["name"])
            for xml_cell, cell in zip(actual, row["cells"]):
                coordinate = cell["coordinate"]
                if coordinate not in anchors or coordinate in safe:
                    continue
                formula = xml_cell.find(S + "f")
                if formula is not None:
                    xml_cell.remove(formula)
                value = xml_cell.find(S + "v")
                if value is None:
                    continue
                if cell["cell_type"] == "inlineStr":
                    xml_cell.remove(value)
                    xml_cell.set("t", "inlineStr")
                    inline = xml.SubElement(xml_cell, S + "is")
                    text = xml.SubElement(inline, S + "t", {XML_SPACE: "preserve"})
                    text.text = value.text
                elif value.text in {None, ""}:
                    xml_cell.remove(value)
        return expected, formula_count, formula_count - len(safe)

    def install_styles(self, styles: bytes):
        if self.style_part is None:
            self.style_part = self._unique_part("xl/reviewStyles.xml")
            self._add_relationship("styles", self.style_part)
            self._declare(self.style_part, "styles")
        self.parts[self.style_part] = styles

    def _unique_part(self, candidate):
        path = PurePosixPath(candidate)
        used = {name.casefold() for name in [*self.original.namelist(), *self.parts]}
        number = 2
        while candidate.casefold() in used:
            candidate = str(path.with_stem(f"{path.stem}{number}"))
            number += 1
        return candidate

    def _add_relationship(self, kind, part):
        used = {node.get("Id") for node in self.workbook_rels}
        index = 1
        while f"rId{index}" in used:
            index += 1
        identifier = f"rId{index}"
        xml.SubElement(self.workbook_rels, REL + "Relationship", Id=identifier,
                       Type=DOCUMENT_RELATIONSHIP_NAMESPACES[0] + "/" + kind, Target="/" + part)
        return identifier

    def _declare(self, part, kind):
        xml.SubElement(
            self.content_types, CT + "Override", PartName="/" + part,
            ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml",
        )

    def add_sheet(self, index: int, name: str, data: bytes):
        part = self._unique_part(f"xl/worksheets/review{index + 1}.xml")
        identifier = self._add_relationship("worksheet", part)
        sheet_id = max((int(node.get("sheetId", "0")) for node in self.entries), default=0) + 1
        self.entries.insert(index, xml.Element(S + "sheet", name=name, sheetId=str(sheet_id), attrib={R + "id": identifier}))
        self._declare(part, "worksheet")
        self.parts[part] = data

    def finish(self) -> bytes:
        self.parts[WORKBOOK] = serialize(self.workbook)
        self.parts[WORKBOOK_RELS] = serialize(self.workbook_rels)
        self.parts[PACKAGE_RELS] = serialize(self.package_rels)
        for node in tuple(self.content_types):
            if node.tag == CT + "Override" and (node.get("PartName") or "").lstrip("/") not in self.parts:
                self.content_types.remove(node)
        self.parts[CONTENT_TYPES] = serialize(self.content_types)
        if sum(map(len, self.parts.values())) > MAX_EXPANDED_BYTES:
            raise WorkbookProblem("output_limit", "The generated workbook exceeds the expanded size limit.")
        buffer = BytesIO()
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as output:
            for name, data in sorted(self.parts.items()):
                output.writestr(name, data)
        data = buffer.getvalue()
        if len(data) > MAX_OUTPUT_BYTES:
            raise WorkbookProblem("output_limit", "The generated workbook exceeds the output size limit.")
        try:
            validate_workbook(data)
            with ZipFile(BytesIO(data)) as output:
                self.verify_source_copy(output)
        except (BadZipFile, UseCaseError) as error:
            raise WorkbookProblem("output_structure_invalid", "The generated workbook could not be reopened.") from error
        return data

    @staticmethod
    def worksheet_geometry(root):
        for view in root.findall(f"{S}sheetViews/{S}sheetView"):
            view.attrib.pop("tabSelected", None)
        links = root.find(S + "hyperlinks")
        if links is not None:
            root.remove(links)
        sheet_data = root.find(S + "sheetData")
        row_structure = []
        if sheet_data is not None:
            for row in sheet_data.findall(S + "row"):
                cell_attributes = [
                    {key: value for key, value in cell.attrib.items() if key != "t"}
                    for cell in row.findall(S + "c")
                ]
                row_structure.append((dict(row.attrib), cell_attributes))
            root.remove(sheet_data)
        return xml.tostring(root, method="c14n"), row_structure

    def verify_source_copy(self, output: ZipFile):
        budget = Budget()
        strings = shared_strings(output, self.strings_part, budget)
        formats = style_formats(output, self.style_part)
        workbook = parse(output.read(WORKBOOK))
        entries = list(workbook.find(S + "sheets"))
        for entry, sheet in zip(entries[-len(self.sheets):], self.sheets):
            expected = sheet.expected
            original = sheet.before
            observed = worksheet_cells(output, original["part"], S, strings, formats, budget)
            observed["comments"] = sheet_comments(output, original["part"], budget)
            original_geometry = self.worksheet_geometry(self.read_xml(original["part"]))
            copied_geometry = self.worksheet_geometry(parse(output.read(original["part"])))
            if (
                entry.get("name") != sheet.output_name or entry.get("state", "visible") != original["visibility"]
                or original_geometry != copied_geometry
                or any(observed[key] != expected[key] for key in observed)
            ):
                raise WorkbookProblem("source_copy_mismatch", "Copied source evidence differs from extraction.", sheet=original["name"])
        for name in output.namelist():
            if name.endswith(".rels") and any(
                relation.get("TargetMode", "").lower() == "external" for relation in parse(output.read(name))
            ):
                raise WorkbookProblem("external_link_detected", "The generated workbook contains an external relationship.")
