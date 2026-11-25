"""Focused retrieval evaluation with invented rules, project labels and amounts."""

from hashlib import sha256

import pytest

from app.rule_retrieval import RuleDocument, search_rules

SPECIFICATION = """# Synthetic approved rules

## 5. Current scope
1. Determine whether Option, Removal, and Deletion items are included in the authorized total.
2. If the user requests base scope only, exclude the source options and adjustments.

## 6. Trade guidance
- FF&E maps to Doors.
- FF&E maps to Doors. Historical example from another project: $98,765.43.
- Classify Access Panel separately from Ceiling and use the source evidence to confirm its type.
- Separate glass, glazing frames, and mullions from Partition where the source prices establish the split.
- Classify genuine repair and patching work under Make Good.
- Use Other only for items that cannot reasonably fit another category.

## 7. Area guidance
Do not invent floor allocations or divide combined amounts evenly without prices.

## 20. Historical evaluations
- FF&E maps to Doors for a past project with 98,765.43 in glazing costs.
- Classify Access Panel separately from Ceiling at the earlier contract price.
"""
DIGEST = sha256(SPECIFICATION.encode()).hexdigest()
DOCUMENT = RuleDocument("SKILL.md", "synthetic-approved-v1", DIGEST)


@pytest.mark.parametrize(("description", "areas", "section", "phrase"), [
    ("FF&E supply", {}, "6", "FF&E maps to Doors."),
    ("Standard Access Panel in suspended ceiling", {}, "6", "Classify Access Panel separately from Ceiling"),
    ("Partitions with glazing frames", {}, "6", "Separate glass, glazing frames, and mullions"),
    ("Floor fit-off", {"Level 1 and Level 2": ["Trades!B4"]}, "7", "Do not invent floor allocations"),
    ("Floor fit-off", {"Level 1 / Level 2": ["Trades!B4"]}, "7", "Do not invent floor allocations"),
    ("Deletion of alternate partition", {}, "5", "Determine whether Option, Removal, and Deletion"),
    ("Temporary fencing", {}, "6", "Use Other only for items"),
    ("Level 1 and patching existing wall", {}, "6", "Classify genuine repair and patching"),
])
def test_expected_rule_retrieval(description, areas, section, phrase):
    inputs = {"rows": [{"row": "Trades!D4", "description": description, "area_candidates": areas}]}
    result = search_rules(SPECIFICATION, DIGEST, DOCUMENT, inputs)
    assert result.status == "matched"
    hit, = result.passages
    assert hit.section == section and phrase in hit.text
    assert (hit.document, hit.version, hit.sha256, hit.row_refs) == (
        DOCUMENT.document, DOCUMENT.version, DOCUMENT.sha256, ("Trades!D4",),
    )
    assert "98,765.43" not in hit.text
    assert "earlier contract price" not in hit.text
    assert result == search_rules(SPECIFICATION, DIGEST, DOCUMENT, inputs)


def test_scope_guidance_can_be_retrieved_for_a_current_excluded_row():
    inputs = {
        "rows": [{"row": "Trades!D4", "description": "Temporary fencing"}],
        "scope_rows": [{"row": "Trades!D5", "description": "Option - alternate material"}],
    }
    result = search_rules(SPECIFICATION, DIGEST, DOCUMENT, inputs)
    hits = {hit.section: hit for hit in result.passages}
    assert hits["5"].row_refs == ("Trades!D5",)
    assert hits["6"].row_refs == ("Trades!D4",)


def test_no_match_and_outdated_document_cannot_contribute_passages():
    unfamiliar = {"rows": [{"row": "Trades!D4", "description": "Site logistics"}]}
    assert search_rules(SPECIFICATION, DIGEST, DOCUMENT, unfamiliar).status == "no_match"
    relevant = {"rows": [{"row": "Trades!D4", "description": "FF&E"}]}
    result = search_rules(SPECIFICATION, "f" * 64, DOCUMENT, relevant)
    assert result.status == "version_mismatch" and result.passages == ()


def test_version_and_passage_ids_survive_index_rebuilds():
    inputs = {"rows": [{"row": "Trades!D4", "description": "Masterkey provisioning"}]}
    first, = search_rules(SPECIFICATION, DIGEST, DOCUMENT, inputs).passages
    revised_text = SPECIFICATION.replace("FF&E maps to Doors.\n", "FF&E maps to Doors. Require current source detail.\n", 1)
    digest = sha256(revised_text.encode()).hexdigest()
    revised = RuleDocument("SKILL.md", "synthetic-approved-v2", digest)
    later, = search_rules(revised_text, digest, revised, inputs).passages
    assert later.section == first.section
    assert later.passage_id != first.passage_id and later.version == "synthetic-approved-v2"
