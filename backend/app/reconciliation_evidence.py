"""Workbook references and the small set of source formula shapes we can trace."""

import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

# Quoted worksheet names are scanned before splitting on formula punctuation.
REFERENCE = r"(?:(?:'(?:[^']|'')+'|[A-Za-z0-9_.]+)!)?\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}"
REF_TOKEN = re.compile(REFERENCE)
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")
SCOPE = re.compile(r"^(option|removal|deletion)\b", re.IGNORECASE)
MAX_REFERENCES = 10_000
OUTSIDE_TOTAL = re.compile(
    r"(?:excluded from|not included in|outside) (?:the )?"
    r"(?:(?:reported )?(?:source|project|control|final) )total"
)


def clean(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def stated_tax_bases(value: str | None) -> set[str]:
    words = clean(value)
    bases = set()
    if re.search(r"\b(?:ex|excl|excluding|exclusive of) gst\b", words):
        bases.add("excluding_gst")
    if re.search(r"\b(?:inc|incl|including|inclusive of) gst\b", words):
        bases.add("including_gst")
    return bases


def tax_basis(value: str | None) -> str | None:
    bases = stated_tax_bases(value)
    return bases.pop() if len(bases) == 1 else None


def source_label_parts(value: str) -> list[str]:
    return [clean(part) for part in re.split(r"[():;–—]|\s-\s", value)]


def explicitly_outside_total(value: str) -> bool:
    return any(OUTSIDE_TOTAL.fullmatch(part) for part in source_label_parts(value))


def total_kind(value: str, sheet: str = "") -> str | None:
    words = " ".join(
        part for part in source_label_parts(value) if part and not OUTSIDE_TOTAL.fullmatch(part)
    )
    if re.search(r"\b(?:subtotal|sub total|sheet total|worksheet total|area total|trade total|section total)\b", words):
        return "subtotal"
    if "total" not in words.split():
        return None
    if re.search(r"\b(?:option|removal|deletion|alternative|alternate)\b", words + " " + clean(sheet)):
        return "alternative"
    if re.search(r"\b(?:grand|project|final|contract) total\b", words):
        return "project"
    if re.fullmatch(
        r"total(?: (?:ex|excl|excluding|exclusive of|inc|incl|including|inclusive of) gst)?", words,
    ):
        return "total"
    return None


def numeric(cell: dict | None, *, literal: bool = False) -> Decimal | None:
    if cell is None or cell.get("cell_type") != "n" or (literal and cell.get("formula")):
        return None
    raw = cell.get("raw_value")
    if not isinstance(raw, str) or len(raw) > 80 or NUMBER.fullmatch(raw) is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value.adjusted()) > 30 or value.as_tuple().exponent < -50:
        return None
    return value


class Reference(NamedTuple):
    sheet: str
    cell: str

    @property
    def key(self) -> str:
        return f"{self.sheet}!{self.cell}"

    @classmethod
    def from_dict(cls, value: dict) -> "Reference":
        return cls(value["sheet"], value["cell"])

    def evidence(self) -> dict:
        return {"sheet": self.sheet, "cell": self.cell}


@dataclass(frozen=True)
class SourceRow:
    amount: Reference
    description: str
    label: Reference | None
    fields: dict[str, Reference]

    @property
    def scope_kind(self) -> str | None:
        match = SCOPE.match(self.description.strip()) or SCOPE.match(self.amount.sheet.strip())
        return match[1].lower() if match else None


@dataclass(frozen=True)
class ControlCandidate:
    row: SourceRow
    kind: str

    def evidence(self) -> dict:
        return {
            "reference": self.row.amount.evidence(), "label": self.row.label.evidence(),
            "description": self.row.description, "kind": self.kind, "gst_basis": tax_basis(self.row.description),
        }


class WorkbookEvidence:
    def __init__(self, extraction: dict):
        self.cells: dict[Reference, dict] = {}
        self.rows: dict[Reference, SourceRow] = {}
        self.tables: list[list[SourceRow]] = []
        self.candidates: list[ControlCandidate] = []
        self.basis_evidence: dict[Reference, str] = {}
        indexed_rows = []
        rows_by_sheet = {}
        for sheet in extraction["raw"]["sheets"]:
            name = sheet["name"]
            rows_by_sheet[name] = sheet["rows"]
            for row in sheet["rows"]:
                indexed_rows.append((name, row["cells"]))
                for cell in row["cells"]:
                    self.cells[Reference(name, cell["coordinate"])] = cell

        headers = defaultdict(list)
        for table in extraction["identified"]["tables"]:
            headers[table["sheet"]].append(table)
        for sheet, tables in headers.items():
            tables.sort(key=lambda table: table["header_row"])
            rows = rows_by_sheet[sheet]
            positions = [row["row"] for row in rows]
            for index, table in enumerate(tables):
                columns = {
                    self.cells[Reference.from_dict(ref)]["column"]: name
                    for name, ref in table["columns"].items()
                }
                if "amount" not in columns.values() or "description" not in columns.values():
                    continue
                start = bisect_right(positions, table["header_row"])
                end = (
                    bisect_right(positions, tables[index + 1]["header_row"] - 1)
                    if index + 1 < len(tables) else len(rows)
                )
                table_rows = []
                # A blank row is not evidence that the source scope has ended.
                for row in rows[start:end]:
                    fields = {
                        columns[cell["column"]]: Reference(sheet, cell["coordinate"])
                        for cell in row["cells"]
                        if cell["column"] in columns and (
                            columns[cell["column"]] == "amount"
                            or any(cell[key] is not None for key in ("raw_value", "text", "formula"))
                        )
                    }
                    if "amount" in fields and "description" in fields:
                        label = fields["description"]
                        text = self.cells[label].get("text")
                        if text:
                            item = SourceRow(fields["amount"], text, label, fields)
                            self.rows[item.amount] = item
                            table_rows.append(item)
                if table_rows:
                    self.tables.append(table_rows)

        seen_candidates = set()
        for sheet, cells in indexed_rows:
            for index, cell in enumerate(cells):
                text = cell.get("text")
                if not text or cell.get("formula"):
                    continue
                label = Reference(sheet, cell["coordinate"])
                if clean(text) in {"gst basis", "tax basis"} and index + 1 < len(cells):
                    basis_cell = cells[index + 1]
                    if basis := tax_basis(basis_cell.get("text")):
                        self.basis_evidence[Reference(sheet, basis_cell["coordinate"])] = basis
                kind = total_kind(text, sheet)
                if kind is None or kind == "subtotal":
                    continue
                amounts = [
                    Reference(sheet, other["coordinate"])
                    for other in cells[index + 1:]
                    if numeric(other) is not None or other.get("formula")
                ]
                declared = [candidate for candidate in amounts if self.rows.get(candidate, None)
                            and self.rows[candidate].label == label]
                for amount in declared or amounts:
                    if amount not in seen_candidates:
                        seen_candidates.add(amount)
                        row = self.rows.get(amount, SourceRow(amount, text, label, {}))
                        self.candidates.append(ControlCandidate(row, kind))
        self.control_rows = {candidate.row.amount: candidate.row for candidate in self.candidates}

    def detail_rows(self):
        return (
            row for row in self.rows.values()
            if total_kind(row.description, row.amount.sheet) is None
            or row.scope_kind is not None
        )

    def row_for(self, ref: Reference) -> SourceRow:
        return self.rows.get(ref) or self.control_rows.get(ref) or SourceRow(ref, "", None, {})


def parse_ref(token: str, sheet: str) -> Reference:
    worksheet, sep, cell = token.rpartition("!")
    if not sep:
        cell, worksheet = token, sheet
    if worksheet.startswith("'"):
        worksheet = worksheet[1:-1].replace("''", "'")
    return Reference(worksheet, cell.replace("$", ""))


def remove_formula_space(value: str) -> str:
    chunks = re.split(r"('(?:[^']|'')+')", value.removeprefix("="))
    return "".join(chunk if index % 2 else re.sub(r"\s+", "", chunk)
                   for index, chunk in enumerate(chunks))


def additive_references(formula: str, sheet: str) -> list[tuple[Reference, int]] | None:
    """Read declared dependencies only. Unsupported formulas have no numeric fallback."""
    expression = remove_formula_space(formula)
    position = 0
    refs = []
    while position < len(expression):
        sign = 1
        if expression[position] in "+-":
            sign = -1 if expression[position] == "-" else 1
            position += 1
        if expression[position:position + 4].upper() == "SUM(":
            position += 4
            inside = True
        else:
            inside = False

        while True:
            match = REF_TOKEN.match(expression, position)
            if match is None:
                return None
            start = parse_ref(match[0], sheet)
            position = match.end()
            if inside and expression[position:position + 1] == ":":
                end = REF_TOKEN.match(expression, position + 1)
                if end is None:
                    return None
                last = parse_ref(end[0], start.sheet)
                start_col, start_row = re.fullmatch(r"([A-Z]+)([0-9]+)", start.cell).groups()
                end_col, end_row = re.fullmatch(r"([A-Z]+)([0-9]+)", last.cell).groups()
                first, final = int(start_row), int(end_row)
                if last.sheet != start.sheet or start_col != end_col or not 0 <= final - first < MAX_REFERENCES:
                    return None
                if len(refs) + final - first + 1 > MAX_REFERENCES:
                    return None
                refs.extend((Reference(start.sheet, f"{start_col}{index}"), sign) for index in range(first, final + 1))
                position = end.end()
            else:
                refs.append((start, sign))
                if len(refs) > MAX_REFERENCES:
                    return None
            if not inside:
                break
            if expression[position:position + 1] == ")":
                position += 1
                break
            if expression[position:position + 1] != ",":
                return None
            position += 1
        if position < len(expression) and expression[position] not in "+-":
            return None
    return refs or None


def markup_recipe(formula: str, sheet: str) -> tuple[list[Reference], Reference] | None:
    compact = remove_formula_space(formula)
    recipe = re.fullmatch(rf"({REFERENCE})(?:\*({REFERENCE}))?\*\(1\+({REFERENCE})\)", compact)
    if recipe is None:
        return None
    return [parse_ref(value, sheet) for value in recipe.groups()[:2] if value], parse_ref(recipe[3], sheet)


def quantity_rate_recipe(formula: str, sheet: str) -> set[Reference] | None:
    compact = remove_formula_space(formula)
    recipe = re.fullmatch(rf"({REFERENCE})\*({REFERENCE})", compact)
    return {parse_ref(value, sheet) for value in recipe.groups()} if recipe else None
