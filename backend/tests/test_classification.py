import hashlib
import json
from decimal import Decimal
from xml.sax.saxutils import escape

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app import classification_model
from app.classification_evidence import ClassificationEvidenceError
from app.classify import Proposal, build_classification, make_evidence
from app.config import Settings
from app.extraction_reader import extract_workbook
from app.reconcile import reconcile
from test_reconciliation import SHEET_NS, SOURCE, WORKSHEET, evidence_for, fixture

SHEET = "Trade Summary"
MAPPINGS = [
    ("FF&E", "Doors"), ("Masterkey", "Doors"),
    ("Confirmed client-supplied material wastage", "Ceiling"),
    ("Patching and repair", "Make Good"), ("Ceiling access panels", "Access Panel"),
    ("Glazing frame installation", "Glazing"), ("Temporary hoarding", "Other"),
]


def proposal_for(evidence, choices=None):
    return Proposal.model_validate({"rows": [
        {
            "row": key, "trade": "Other", "trade_evidence": [row.label.key],
            "reason": "The current source description supports this proposal.",
            "area": None, "glazing_price": None,
            **(choices or {}).get(key, {}),
        }
        for key, row in evidence.rows.items()
    ]})


def reconciled(extraction, scope=SOURCE):
    report = reconcile(extraction, scope, {})
    assert report["status"] == "ready_for_classification", report
    return make_evidence(extraction, report)


def test_model_mappings_retain_protected_amounts_and_negative_rows(workbook_with):
    source = evidence_for(workbook_with, [
        *[(description, "-5" if index == 1 else "10", None)
          for index, (description, _) in enumerate(MAPPINGS)],
        ("Project Total ex GST", "55", "SUM(D4:D10)"),
    ])
    evidence = reconciled(source)
    output = build_classification(evidence, proposal_for(evidence, {
        f"{SHEET}!D{index}": {"trade": trade} for index, (_, trade) in enumerate(MAPPINGS, 4)
    }), {}, "proposal")
    assert output["status"] == "ready_for_allocation"
    assert output["classified_total"] == "55.00"
    assert [item["trade"] for item in output["assignments"]] == [trade for _, trade in MAPPINGS]
    assert output["assignments"][1]["amount"] == "-5"
    assert output["assignments"][1]["trade_evidence"] == [f"{SHEET}!A5"]
    assert {item["trade"] for item in output["assignments"] if item["protected_by_default"]} == {
        "Make Good", "Access Panel",
    }
    assert all(item["area"] == "Additional Items" for item in output["assignments"])
    assert all(item["trade_origin"] == "model_proposed" for item in output["assignments"])


@pytest.mark.parametrize("description,trade", MAPPINGS[:-1])
def test_explicit_misclassification_requires_review(workbook_with, description, trade):
    evidence = reconciled(evidence_for(workbook_with, [
        (description, "10", None), ("Project Total ex GST", "10", "D4"),
    ]))
    proposals = proposal_for(evidence)
    blocked = build_classification(evidence, proposals, {}, "proposal")
    question, = blocked["questions"]
    assert blocked["status"] == "blocked"
    assert question["kind"] == "trade" and trade in question["prompt"]
    assert question["evidence"] == [f"{SHEET}!A4"]
    ready = build_classification(evidence, proposals, {question["key"]: {"answer": trade}}, "proposal")
    assert ready["status"] == "ready_for_allocation"
    assert ready["assignments"][0]["trade_origin"] == "operator_confirmed"


@pytest.mark.parametrize(
    "case,code", [
        ("duplicated", "proposal_coverage_mismatch"), ("missing", "proposal_coverage_mismatch"),
        ("invented", "proposal_coverage_mismatch"), ("false_evidence", "invalid_trade_evidence"),
        ("other_row_evidence", "invalid_trade_evidence"),
    ],
)
def test_mechanical_checks_reject_bad_coverage_and_references(workbook_with, case, code):
    evidence = reconciled(evidence_for(workbook_with, [
        ("Temporary hoarding", "10", None), ("Adjustment", "-3", None), ("Zero item", "0", None),
        ("Project Total ex GST", "7", "SUM(D4:D6)"),
    ]))
    rows = proposal_for(evidence).model_dump()["rows"]
    if case == "duplicated":
        rows[1] = rows[0]
    elif case == "missing":
        rows.pop()
    elif case == "invented":
        rows[1]["row"] = f"{SHEET}!D7"
    elif case == "false_evidence":
        rows[0]["trade_evidence"].append(f"{SHEET}!Z99")
    else:
        rows[0]["trade_evidence"].append(f"{SHEET}!A5")
    with pytest.raises(ClassificationEvidenceError) as error:
        build_classification(evidence, Proposal.model_validate({"rows": rows}), {}, "proposal")
    assert error.value.code == code


def component_workbook(
    workbook_with, component="33", area="Level 1", price_header="Glazing Amount Incl Markup",
    parent="Partition including glazing",
):
    def text(col, row, value):
        return f'<c r="{col}{row}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    headers = {"A": "Description", "B": "Base", "C": "Markup", "D": "Amount", "E": price_header, "F": "Area"}
    return workbook_with({WORKSHEET: f"""
<worksheet xmlns="{SHEET_NS}"><sheetData>
<row r="3">{"".join(text(col, 3, value) for col, value in headers.items())}</row>
<row r="4">{text("A", 4, parent)}<c r="B4"><v>100</v></c><c r="C4"><v>0.1</v></c>
<c r="D4"><f>B4*(1+C4)</f><v>110</v></c><c r="E4"><v>{component}</v></c>{text("F", 4, area)}</row>
<row r="5">{text("A", 5, "Project Total ex GST")}<c r="D5"><f>D4</f><v>110</v></c></row>
</sheetData></worksheet>""".encode()})


@pytest.mark.parametrize("source_area,proposed_area", [
    ("Level 1 and Level 2", "Level 1"),
    ("Level 1 and Level 2", "Level 1 and Level 2"),
    ("Level 1 and Level 2", None),
    ("Levels 1–2", None),
    ("Ground Floor / Level 2", None),
])
def test_residual_split_keeps_markup_and_combined_area(workbook_with, source_area, proposed_area):
    evidence = reconciled(extract_workbook(component_workbook(workbook_with, area=source_area)))
    proposals = proposal_for(evidence, {
        f"{SHEET}!D4": {"trade": "Partition", "glazing_price": f"{SHEET}!E4", "area": proposed_area},
    })
    draft = build_classification(evidence, proposals, {}, "proposal")
    question, = draft["questions"]
    assert draft["status"] == "blocked" and draft["assignments"] == []
    assert question["kind"] == "area"
    assert question["options"] == [source_area]
    assert question["evidence"] == [f"{SHEET}!F4", f"{SHEET}!F3"]
    report = build_classification(evidence, proposals, {question["key"]: {"answer": question["options"][0]}}, "proposal")
    assert report["status"] == "ready_for_allocation"
    assert report["classified_total"] == "110.00"
    assert report["row_coverage"] == [{
        "row": f"{SHEET}!D4", "working_amount": "110.0", "assigned_amount": "110.0", "assignment_count": 2,
    }]
    glazing, partition = report["assignments"]
    assert (glazing["trade"], glazing["amount"]) == ("Glazing", "33")
    assert partition["trade"] == "Partition" and Decimal(partition["amount"]) == Decimal("77")
    for piece in (glazing, partition):
        assert piece["area"] == source_area
        assert piece["area_evidence"] == [f"{SHEET}!F4", f"{SHEET}!F3"]
        assert piece["area_origin"] == "operator_confirmed"
        assert piece["markup_treatment"] == "included"
        assert piece["arithmetic"]["component"] == f"{SHEET}!E4"
        assert piece["arithmetic"]["parent"] == f"{SHEET}!D4"
        assert piece["arithmetic"]["inclusion_reference"] == f"{SHEET}!A4"
    assert partition["arithmetic"]["operation"] == "parent_minus_component"


@pytest.mark.parametrize(
    "component,header,pointer", [
        ("111", "Glazing Amount Incl Markup", "E4"),
        ("33", "Glazing Amount Excl Markup", "E4"),
        ("33", "Glazing Amount Incl Markup", "D4"),
    ],
)
def test_invalid_residual_asks_without_adding_money(workbook_with, component, header, pointer):
    evidence = reconciled(extract_workbook(component_workbook(workbook_with, component, price_header=header)))
    proposal = proposal_for(evidence, {f"{SHEET}!D4": {"trade": "Partition", "glazing_price": f"{SHEET}!{pointer}"}})
    blocked = build_classification(evidence, proposal, {}, "proposal")
    assert blocked["status"] == "blocked" and blocked["assignments"] == []
    assert blocked["questions"][0]["kind"] == "combined_trade"
    assert f"{SHEET}!{pointer}" not in blocked["questions"][0]["options"]


@pytest.mark.parametrize("parent", [
    "Partition", "Partition (excludes glazing)",
    "Partition (glazing not included)", "Partition does not include any glazing",
])
def test_same_markup_is_insufficient_to_infer_glazing_is_inside_parent(workbook_with, parent):
    evidence = reconciled(extract_workbook(component_workbook(workbook_with, parent=parent)))
    proposal = proposal_for(evidence, {f"{SHEET}!D4": {"trade": "Partition", "glazing_price": f"{SHEET}!E4"}})
    report = build_classification(evidence, proposal, {}, "proposal")
    question, = report["questions"]
    assert report["status"] == "blocked" and report["assignments"] == []
    assert question["kind"] == "combined_trade"
    assert f"{SHEET}!E4" not in question["options"]
    assert {f"{SHEET}!A4", f"{SHEET}!E4", f"{SHEET}!E3"} == set(question["evidence"])


def test_explicit_component_header_can_confirm_inclusion_but_cannot_override_exclusion(workbook_with):
    header = "Glazing Amount Incl Markup Included in Partition"
    for parent, expected in [("Partition", "ready_for_allocation"), ("Partition without glazing", "blocked")]:
        evidence = reconciled(extract_workbook(component_workbook(workbook_with, parent=parent, price_header=header)))
        proposal = proposal_for(evidence, {f"{SHEET}!D4": {
            "trade": "Partition", "glazing_price": f"{SHEET}!E4", "area": "Level 1",
        }})
        report = build_classification(evidence, proposal, {}, "proposal")
        assert report["status"] == expected
        if expected == "ready_for_allocation":
            assert report["assignments"][0]["arithmetic"]["inclusion_reference"] == f"{SHEET}!E3"
        else:
            assert report["questions"][0]["options"] == ["keep:Partition"]


@pytest.mark.parametrize("description", [
    "Level 2 Perforated Access Panel", "Ground Floor Feature Access Panel",
    "Level 4 supply and install PV access panels", "Option - Ceiling Access Panel (ex GST)",
])
def test_explicit_access_panel_variants_cannot_become_unprotected_without_confirmation(workbook_with, description):
    evidence = reconciled(evidence_for(workbook_with, [
        (description, "10", None), ("Project Total ex GST", "10", "D4"),
    ]))
    report = build_classification(evidence, proposal_for(evidence), {}, "proposal")
    question, = report["questions"]
    assert report["status"] == "blocked" and report["assignments"] == []
    assert "Access Panel" in question["prompt"]


def test_approved_fixture_standard_access_panel_remains_protected():
    evidence = reconciled(fixture("reconciliation.xlsx"))
    row = f"{SHEET}!D11"
    proposal = proposal_for(evidence)
    draft = build_classification(evidence, proposal, {}, "proposal")
    trade_question, = [item for item in draft["questions"] if item["row"] == row]
    assert "Access Panel" in trade_question["prompt"]
    assert not any(item["row"] == row for item in draft["assignments"])
    answer = {trade_question["key"]: {"answer": "Access Panel"}}
    corrected = build_classification(evidence, proposal, answer, "proposal")
    item, = [item for item in corrected["assignments"] if item["row"] == row]
    assert Decimal(item["amount"]) == Decimal("20.00")
    assert item["protected_by_default"] is True and item["trade_origin"] == "operator_confirmed"


def test_model_amount_fields_are_rejected(workbook_with):
    evidence = reconciled(evidence_for(workbook_with, [
        ("Other work", "7", None), ("Project Total ex GST", "7", "D4"),
    ]))
    rows = proposal_for(evidence).model_dump()["rows"]
    rows[0]["amount"] = "1.00"
    with pytest.raises(ValidationError):
        Proposal.model_validate({"rows": rows})


def test_explicit_scope_exclusions_and_additions_are_not_inferred(workbook_with):
    extraction = evidence_for(workbook_with, [
        ("Temporary hoarding", "100", None),
        ("Option - Access Panel (ex GST)", "15", None),
        ("Deletion - Joinery", "-5", None),
        ("Project Total ex GST", "95", "D4+D6"),
    ])
    reference = f"{SHEET}!D5"
    before = reconciled(extraction)
    assert set(before.rows) == {f"{SHEET}!D4", f"{SHEET}!D6"}
    after = reconciled(extraction, {"mode": "source", "include": [reference], "exclude": [f"{SHEET}!D6"]})
    report = build_classification(after, proposal_for(after, {reference: {"trade": "Access Panel"}}), {}, "proposal")
    assert report["classified_total"] == "115.00"
    panel = next(item for item in report["assignments"] if item["row"] == reference)
    assert panel["scope_origin"] == "authorized_addition" and panel["protected_by_default"]


def write_skill(directory):
    skill = directory / "SKILL.md"
    skill.write_text(
        "# Synthetic test specification\n\n## 6. Trades\nFF&E maps to Doors.\n"
        "## 7. Areas\nPreserve combined areas.\n## 8. Protection\nInspect source.\n"
        "## 11. QA\nCheck evidence.\n",
        encoding="utf-8",
    )
    return skill


def write_codex(directory, monkeypatch):
    codex = directory / "Invoice.Breakdown.Codex.md"
    codex.write_text(
        "## 三、Scope\nOmit this preceding section.\n"
        "## 四、Trade\nVerified synthetic trade rules.\n"
        "## 五、Area\nVerified synthetic area rules.\n"
        "## 六、GST\nOmit this following section.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(classification_model, "APPROVED_CODEX_SHA256", hashlib.sha256(codex.read_bytes()).hexdigest())
    return codex


def test_primary_skill_works_without_supplementary_file(tmp_path):
    skill = write_skill(tmp_path)
    result = classification_model.read_primary_specification(skill)
    assert result.sha256 == hashlib.sha256(skill.read_bytes()).hexdigest()
    assert result.text == skill.read_text()
    assert result.supplementary.status == "not_configured"
    missing = classification_model.read_primary_specification(skill, tmp_path / "missing.md")
    assert missing.supplementary.status == "unavailable"
    mismatched = classification_model.read_primary_specification(skill, skill)
    assert mismatched.supplementary.status == "checksum_mismatch" and mismatched.supplementary.text is None
    with pytest.raises(classification_model.ClassificationModelError, match="primary_specification_unavailable"):
        classification_model.read_primary_specification(tmp_path / "other" / "SKILL.md")


@pytest.mark.anyio
@pytest.mark.parametrize("result", ["success", "refusal", "rate_limit", "null_message", "null_content"])
async def test_provider_adapter_handles_result_and_failures(monkeypatch, tmp_path, result):
    config = Settings(_env_file=None, classification_model="example-model", llm_api_key=SecretStr("example-key"))
    codex = write_codex(tmp_path, monkeypatch)
    specification = classification_model.read_primary_specification(write_skill(tmp_path), codex)
    assert specification.supplementary.provenance() == {
        "status": "applied", "version": "2026-10-05",
        "sha256": hashlib.sha256(codex.read_bytes()).hexdigest(), "sections": ["IV", "V"],
    }
    client_class = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["response_format"]["json_schema"]["strict"] is True
        instruction = payload["messages"][0]["content"]
        assert specification.text in instruction
        assert "Verified synthetic trade rules." in instruction and "Verified synthetic area rules." in instruction
        assert "Omit this" not in instruction
        assert json.loads(payload["messages"][1]["content"]) == {"rows": []}
        if result == "rate_limit":
            return httpx.Response(429, text="sensitive upstream error")
        message = {
            "content": None if result == "null_content" else '{"rows":[]}',
            "refusal": "refused" if result == "refusal" else None,
        } if result != "null_message" else None
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": message}]})

    monkeypatch.setattr(classification_model.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    if result == "success":
        assert (await classification_model.propose(config, specification, {"rows": []})).rows == []
    else:
        with pytest.raises(classification_model.ClassificationModelError) as failure:
            await classification_model.propose(config, specification, {"rows": []})
        assert failure.value.code == ("model_unavailable" if result == "rate_limit" else "invalid_model_proposal")
