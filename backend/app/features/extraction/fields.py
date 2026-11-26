"""Identify explicitly labelled evidence within each workbook's own layout."""

from collections import defaultdict

METADATA = {
    "job name": "project_name",
    "project name": "project_name",
    "job address": "project_address",
    "project address": "project_address",
    "start date": "start_date",
    "report date": "report_date",
    "quotation date": "quotation_date",
    "estimate date": "estimate_date",
    "tax basis": "tax_basis",
    "gst basis": "tax_basis",
}
COLUMNS = {
    "description": "description",
    "item description": "description",
    "work description": "description",
    "quantity": "quantity",
    "qty": "quantity",
    "unit": "unit",
    "uom": "unit",
    "unit of measure": "unit",
    "rate": "rate",
    "unit rate": "rate",
    "unit price": "rate",
    "markup": "markup",
    "mark up": "markup",
    "markup %": "markup",
    "amount": "amount",
    "base": "base",
    "total": "amount",
}
ANNOTATIONS = {
    "note": "note", "notes": "note",
    "exclusion": "exclusion", "exclusions": "exclusion", "scope exclusions": "exclusion",
}


def label(cell: dict) -> str:
    if cell["formula"] is not None or cell["text"] is None:
        return ""
    return " ".join(cell["text"].strip().lower().rstrip(":").split())


def has_value(cell: dict) -> bool:
    return any(cell[key] is not None for key in ("text", "raw_value", "formula"))


def reference(sheet: dict, cell: dict) -> dict:
    return {"sheet": sheet["name"], "cell": cell["coordinate"]}


def identify_fields(sheets: list[dict]) -> dict:
    metadata = defaultdict(list)
    annotated = []
    tables = []

    for sheet in sheets:
        table = None
        active_columns = {}
        section_annotation = None
        sheet_annotation = ANNOTATIONS.get(sheet["name"].strip().lower())
        for row in sheet["rows"]:
            populated = [cell for cell in row["cells"] if has_value(cell)]
            if not populated:
                table = None
                section_annotation = None
                continue
            labels = [label(cell) for cell in populated]
            candidates = defaultdict(list)
            for cell, key in zip(populated, labels):
                if field := COLUMNS.get(key):
                    candidates[field].append(cell)
            # Require a description heading plus another unambiguous field.
            columns = {field: cells[0] for field, cells in candidates.items() if len(cells) == 1}
            if "description" in columns and len(columns) > 1:
                section_annotation = None
                table = {
                    "sheet": sheet["name"], "header_row": row["row"],
                    "columns": {field: reference(sheet, cell) for field, cell in columns.items()},
                    "unresolved_columns": sorted(field for field, cells in candidates.items() if len(cells) > 1),
                    "rows": [],
                }
                tables.append(table)
                active_columns = {cell["column"]: field for field, cell in columns.items()}
                continue

            row_annotation = next((ANNOTATIONS[key] for key in labels if key in ANNOTATIONS), None)
            labelled_metadata = any(key in METADATA for key in labels)
            if labelled_metadata:
                section_annotation = None
            elif row_annotation and len(populated) == 1:
                section_annotation = row_annotation
            if row_annotation or labelled_metadata:
                table = None
            for index, (cell, key) in enumerate(zip(populated, labels)):
                if field := METADATA.get(key):
                    next_cell = populated[index + 1] if index + 1 < len(populated) else None
                    next_label = labels[index + 1] if next_cell is not None else ""
                    if next_cell is not None and (
                        next_cell["column"] != cell["column"] + 1
                        or next_label in METADATA or next_label in ANNOTATIONS or next_label in COLUMNS
                    ):
                        next_cell = None
                    metadata[field].append({
                        "label": reference(sheet, cell),
                        "value": reference(sheet, next_cell) if next_cell is not None else None,
                    })

                annotation = row_annotation or section_annotation or sheet_annotation
                if annotation and key not in ANNOTATIONS:
                    annotated.append({"kind": annotation, "source": reference(sheet, cell)})

            if table is not None:
                fields = {
                    active_columns[cell["column"]]: reference(sheet, cell)
                    for cell in populated
                    if cell["column"] in active_columns
                }
                if "description" in fields and len(fields) > 1:
                    table["rows"].append({"row": row["row"], "fields": fields})
                else:
                    table = None

    return {
        "metadata": {
            field: {
                "status": "labelled" if len(candidates) == 1 and candidates[0]["value"] else "unresolved",
                "candidates": candidates,
            }
            for field, candidates in metadata.items()
        },
        "tables": tables,
        "annotations": annotated,
    }
