"""Source context and arithmetic eligible for classification of reconciled rows."""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.reconcile import cents
from app.reconciliation_evidence import SCOPE, Reference, WorkbookEvidence, clean, numeric

AREA_HEADERS = {"area", "floor", "level", "floor area", "area floor", "location", "zone"}
AREA_HEADING = re.compile(r"^(?:area|floor|level|zone)\s*:\s*(\S.*)$", re.I)
FLOOR_LABEL = r"(?:(?:levels?|floors?)\s+\w+|(?:ground|lower ground|upper ground|first|second|third) floor|basement(?:\s+\w+)?)"
FLOOR_HEADING = re.compile(
    rf"^{FLOOR_LABEL}(?:\s*(?:and|&|/|\+|,|–|-)\s*(?:{FLOOR_LABEL}|[0-9]+[A-Za-z]?))*$", re.I,
)
GLAZING_COMPONENT = r"(?:glazing(?: frames?)?|glass|mullions?)"
GLAZING = re.compile(rf"\b{GLAZING_COMPONENT}\b")
GLAZING_EXCLUDED = re.compile(
    rf"\b(?:(?:excludes?|excluding|excl|ex|without|no|not (?:include|including|with)) "
    rf"(?:(?:the|all|any) )?{GLAZING_COMPONENT}"
    rf"|{GLAZING_COMPONENT} (?:(?:is|are) )?(?:excluded|not included|by others|separate|omitted))\b",
)
GLAZING_INCLUDED = re.compile(
    rf"(?:combined )?partition (?:and|with|includes?|including|incl) (?:the )?{GLAZING_COMPONENT}"
    r"(?: (?:works|amount|subtotal|total))?",
)
GLAZING_PRICE = re.compile(
    rf"{GLAZING_COMPONENT} (?:amount|price)(?: (?:incl(?:uding)?|excl(?:uding)?) markup)?"
    r"(?: included in partition(?: (?:amount|subtotal|total))?)?",
)
MARKUP_BASIS = {"included": r"\bincl(?:uding)? markup\b", "excluded": r"\bexcl(?:uding)? markup\b"}
PANEL = re.compile(r"\baccess panels?\b")
PANEL_LABEL = re.compile(
    rf"(?:{FLOOR_LABEL} )?(?:(?:supply and install|supply of|installation of|install|provide) )?"
    rf"(?:{FLOOR_LABEL} )?(?:ceiling )?(?:(?:standard|perforated|feature|pv) )?access panels?"
    r"(?: (?:allowance|adjustment|supply|installation|credit))?",
)
EXCLUSION = re.compile(r"\b(?:exclud\w*|not (?:included|allowed|provided)|by others|omitted|no access panels?)\b")
NO_EXCLUSION = re.compile(r"\b(?:not excluded|no exclusions?)\b")


class ClassificationEvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class WorkingRow:
    reference: Reference
    label: Reference
    description: str
    amount: Decimal
    coefficient: int
    parent: Reference | None
    markup: str
    markup_evidence: list[dict]
    scope_origin: str


def working_rows(report: dict, workbook: WorkbookEvidence) -> dict[str, WorkingRow]:
    selected = report["selected_rows"]
    additions = report["scope_additions"]
    selected_refs = [Reference.from_dict(row["reference"]) for row in selected]
    excluded = {Reference.from_dict(row["reference"]) for row in report["scope_exclusions"]}
    addition_refs = [Reference.from_dict(row["reference"]) for row in additions]
    all_refs = selected_refs + addition_refs
    if len(set(all_refs)) != len(all_refs) or not excluded.issubset(selected_refs):
        raise ClassificationEvidenceError("invalid_working_scope")

    rows = {}
    for row, reference in zip(selected + additions, all_refs):
        if reference in excluded:
            continue
        source = workbook.row_for(reference)
        if source.label is None or reference not in workbook.cells:
            raise ClassificationEvidenceError("invalid_working_scope")
        addition = "effect" in row
        rows[reference.key] = WorkingRow(
            reference, source.label, row["description"],
            Decimal(row["effect"] if addition else row["contribution"]),
            row.get("coefficient", 1), Reference.from_dict(row["parent"]) if row.get("parent") else None,
            row["markup_treatment"], row["markup_evidence"],
            "authorized_addition" if addition else "in_source",
        )
    if cents(sum((row.amount for row in rows.values()), Decimal(0))) != Decimal(report["working_scope_total"]):
        raise ClassificationEvidenceError("working_scope_total_mismatch")
    return rows


def row_number(reference: Reference) -> int:
    return int(reference.cell.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def stated_trade(description: str) -> str | None:
    """Catch conflicts with narrowly phrased, explicit rule labels."""
    words = clean(SCOPE.sub("", description.strip()))
    words = re.sub(r" (?:ex|excl|excluding|inc|incl|including) gst$", "", words)
    if re.fullmatch(r"(?:ff e|ffe|master ?key)(?: (?:allowance|adjustment|supply|installation))?", words):
        return "Doors"
    if re.fullmatch(r"(?:confirmed )?client supplied (?:materials? )?wastage", words):
        return "Ceiling"
    if re.fullmatch(r"(?:make good|patch(?:ing)? and repair(?:s)?|repair(?:s)? and patch(?:ing)?)", words):
        return "Make Good"
    if PANEL_LABEL.fullmatch(words):
        return "Access Panel"
    if re.fullmatch(r"(?:glazing|glass|glazing frames?|mullions?)(?: (?:allowance|adjustment|supply|installation))?", words):
        return "Glazing"
    return None


@dataclass(frozen=True)
class RowEvidence:
    text: dict[str, str]
    areas: dict[str, list[str]]
    cells: dict[int, Reference]
    headers: dict[int, Reference]


class ClassificationEvidence:
    def __init__(self, extraction: dict, report: dict):
        self.workbook = WorkbookEvidence(extraction)
        self.rows = working_rows(report, self.workbook)
        self.total = Decimal(report["working_scope_total"])
        self.gst_basis = report["gst_basis"]
        # Excluded and outside rows cannot be reintroduced as residual detail.
        self.scope_refs = {
            Reference.from_dict(item["reference"])
            for collection in ("selected_rows", "scope_additions", "outside_source_scope", "hierarchy")
            for item in report[collection]
        }
        self.text = {
            ref: cell["text"].strip() for ref, cell in self.workbook.cells.items()
            if cell.get("text") and cell["text"].strip() and not cell.get("formula")
        }
        self.row_context: dict[str, RowEvidence] = {}
        self.notes = []
        self._index(extraction)

    def _index(self, extraction: dict):
        annotations = {
            Reference.from_dict(item["source"]): item["kind"]
            for item in extraction["identified"]["annotations"]
        }
        work_by_sheet_row = {
            (row.reference.sheet, row_number(row.reference)): row for row in self.rows.values()
        }
        for sheet in extraction["raw"]["sheets"]:
            name = sheet["name"]
            headers = {
                table["header_row"] for table in extraction["identified"]["tables"] if table["sheet"] == name
            }
            sheet_area = name if FLOOR_HEADING.fullmatch(name.strip()) else None
            header_cells, area_heading = {}, None
            header_row = 0
            for row in sheet["rows"]:
                cells = {cell["column"]: Reference(name, cell["coordinate"]) for cell in row["cells"]}
                labelled = [ref for ref in cells.values() if ref in self.text]
                if row["row"] in headers:
                    if area_heading and row_number(area_heading[1]) < header_row:
                        area_heading = None
                    header_row = row["row"]
                    header_cells = {column: ref for column, ref in cells.items() if ref in self.text}
                elif len(labelled) == 1 and all(
                    cell.get("raw_value") is None and cell.get("formula") is None
                    for cell in row["cells"] if Reference(name, cell["coordinate"]) not in self.text
                ):
                    ref = labelled[0]
                    heading = AREA_HEADING.fullmatch(self.text[ref])
                    if heading or FLOOR_HEADING.fullmatch(self.text[ref]):
                        area_heading = (heading[1] if heading else self.text[ref], ref)
                for ref in labelled:
                    if ref in annotations:
                        self.notes.append({"reference": ref.key, "text": self.text[ref], "kind": annotations[ref]})
                if work := work_by_sheet_row.get((name, row["row"])):
                    columns = []
                    for column, ref in cells.items():
                        header = header_cells.get(column)
                        if header is None or clean(self.text[header]) not in AREA_HEADERS:
                            continue
                        value = self.text.get(ref)
                        cell = self.workbook.cells[ref]
                        if value is None and re.fullmatch(r"[0-9]{1,4}", cell.get("raw_value") or ""):
                            if numeric(cell, literal=True) is not None:
                                value = cell["raw_value"]
                        if value and len(value) <= 255:
                            columns.append((value, ref, header))
                    labels = columns or ([(area_heading[0], area_heading[1], None)] if area_heading else [])
                    areas = {value: [ref.key, *([header.key] if header else [])] for value, ref, header in labels}
                    if not areas and sheet_area:
                        areas[sheet_area] = []  # The sheet name is retained in the row reference.
                    context = {ref.key: self.text[ref] for ref in [*labelled, *header_cells.values()]}
                    if area_heading:
                        context[area_heading[1].key] = self.text[area_heading[1]]
                    parent_label = self.workbook.row_for(work.parent).label if work.parent else None
                    if parent_label in self.text:
                        context[parent_label.key] = self.text[parent_label]
                    context[work.label.key] = work.description
                    self.row_context[work.reference.key] = RowEvidence(context, areas, cells, header_cells)
            for note in sheet.get("comments", []):
                self.notes.append({
                    "reference": Reference(name, note["coordinate"]).key,
                    "text": note["text"], "kind": note["kind"],
                })

    def panel_exclusions(self):
        return [
            note for note in self.notes
            if PANEL.search(words := clean(note["text"]))
            and (note["kind"] == "exclusion" or EXCLUSION.search(words))
            and not NO_EXCLUSION.search(words)
        ]

    def glazing_prices(self, row: WorkingRow) -> dict[str, dict]:
        """Keep component price evidence even when it cannot support a split."""
        if "partition" not in clean(row.description).split():
            return {}
        context = self.row_context[row.reference.key]
        description = clean(row.description)
        excluded = GLAZING_EXCLUDED.search(description)
        included = GLAZING_INCLUDED.fullmatch(description)
        prices = {}
        for column, header in context.headers.items():
            price = context.cells.get(column)
            label = clean(self.text[header])
            if price is None or not GLAZING_PRICE.fullmatch(label):
                continue
            cell = self.workbook.cells[price]
            if cell.get("raw_value") is None and not cell.get("formula"):
                continue
            amount = numeric(cell, literal=True)
            signed = amount.copy_negate() if amount is not None and row.coefficient == -1 else amount
            basis = MARKUP_BASIS.get(row.markup)
            same_basis = re.search(basis, label) is not None if basis else "markup" not in label.split()
            inclusion_reference = None
            if not excluded:
                if included:
                    inclusion_reference = row.label.key
                elif " included in partition" in label:
                    inclusion_reference = header.key
            supported = (
                inclusion_reference and price not in self.scope_refs and same_basis and signed
                and signed.is_signed() == row.amount.is_signed() and signed.copy_abs() < row.amount.copy_abs()
            )
            prices[price.key] = {
                "price": price.key, "label": header.key, "inclusion_reference": inclusion_reference,
                "amount": signed if supported else None,
            }
        return prices

    def model_input(self) -> dict:
        return {
            "rows": [
                {
                    "row": key,
                    "description": row.description,
                    "description_reference": row.label.key,
                    "text_evidence": self.row_context[key].text,
                    "area_candidates": self.row_context[key].areas,
                    "glazing_price_evidence": [
                        {
                            "price": piece["price"], "label": piece["label"], "eligible": piece["amount"] is not None,
                            "inclusion_reference": piece["inclusion_reference"],
                        }
                        for piece in self.glazing_prices(row).values()
                    ],
                }
                for key, row in self.rows.items()
            ],
            "scope_notes": self.notes,
        }
