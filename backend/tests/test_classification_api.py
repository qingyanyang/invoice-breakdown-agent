import asyncio
import hashlib

import httpx
import pytest
from pydantic import SecretStr

from app import classification_model, classifications
from app.checkpoints import setup_checkpoints
from app.classify import Proposal
from app.config import settings
from test_classification import SHEET, component_workbook, write_codex, write_skill
from test_reconciliation import SHEET_NS, WORKSHEET, source_sheet
from test_reconciliation_api import ALICE, BOB, create_source, establish_evidence, service, success

START = {"expected_revision": 2}


def model_answer(inputs, by_row):
    return Proposal.model_validate({"rows": [
        {
            "row": row["row"], "trade": by_row.get(row["row"], "Other"),
            "trade_evidence": [row["description_reference"]],
            "reason": "The specific workbook description supports this trade.",
            "area": None, "glazing_price": None,
        }
        for row in inputs["rows"]
    ]})


@pytest.fixture
def configured_skill(monkeypatch, tmp_path):
    skill = write_skill(tmp_path)
    monkeypatch.setattr(settings, "classification_skill_path", skill)
    monkeypatch.setattr(settings, "classification_skill_sha256", hashlib.sha256(skill.read_bytes()).hexdigest())
    monkeypatch.setattr(settings, "classification_codex_path", None)
    monkeypatch.setattr(settings, "classification_model", "example-model")
    return skill


async def ready_task(client, workbook):
    task_id = await create_source(client, "classification.xlsx", workbook)
    await establish_evidence(client, task_id)
    reconciliation = success(await client.post(
        f"/tasks/{task_id}/reconciliation", headers=ALICE, json=START,
    ))
    assert reconciliation["status"] == "ready_for_classification"
    return task_id, reconciliation


@pytest.mark.anyio
async def test_classification_resumes_persisted_scope_answer_after_restart(
    task_runtime, configured_skill, monkeypatch, workbook_with, tmp_path,
):
    codex = write_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "classification_codex_path", codex)

    async def model(_settings, spec, inputs):
        assert spec.sha256 == hashlib.sha256(configured_skill.read_bytes()).hexdigest()
        return model_answer(inputs, {f"{SHEET}!D4": "Access Panel"})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({
        WORKSHEET: source_sheet([
            ("Access Panel", "12", None), ("Temporary fencing", "88", None),
            ("Project Total ex GST", "100", "SUM(D4:D5)"),
        ]),
        "xl/worksheets/sheet2.xml": f"""
<worksheet xmlns="{SHEET_NS}"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Access Panels are excluded.</t></is></c></row>
</sheetData></worksheet>""".encode(),
    })
    await setup_checkpoints(settings)
    async with service() as client:
        task_id = await create_source(client, "classification.xlsx", source)
        path = f"/tasks/{task_id}/classification"
        assert (await client.post(path, headers=ALICE, json=START)).status_code == 409
        await establish_evidence(client, task_id)
        assert (await client.post(path, headers=ALICE, json=START)).status_code == 409
        reconciliation = success(await client.post(
            f"/tasks/{task_id}/reconciliation", headers=ALICE, json=START,
        ))
        assert (await client.post(path, headers=BOB, json=START)).status_code == 404
        draft = success(await client.post(path, headers=ALICE, json=START))
        assert draft["status"] == "blocked"
        assert draft["reconciliation_generation"] == reconciliation["generation"]
        assert draft["skill_sha256"] == hashlib.sha256(configured_skill.read_bytes()).hexdigest()
        assert draft["report"]["supplementary_rules"] == {
            "status": "applied", "version": "2026-10-05",
            "sha256": hashlib.sha256(codex.read_bytes()).hexdigest(), "sections": ["IV", "V"],
        }
        question, = draft["report"]["questions"]
        assert question["kind"] == "access_panel_scope"
        assert question["evidence"] == [f"{SHEET}!A4", f"{SHEET}!D4", "Notes!A1"]

    async def unavailable(*_args):
        pytest.fail("A stored classification must resume without another model call")

    monkeypatch.setattr(classification_model, "propose", unavailable)
    # Simulate restarting the application without its model or specification credentials.
    monkeypatch.setattr(settings, "classification_skill_path", None)
    monkeypatch.setattr(settings, "classification_codex_path", None)
    async with service() as client:
        answer = {
            **START, "question_id": question["id"], "answer": "retain_scope",
            "reason": "The priced control includes the authorized panel allowance.",
        }
        assert success(await client.get(path, headers=ALICE)) == draft
        assert (await client.get(path, headers=BOB)).status_code == 404
        assert (await client.post(path + "/answers", headers=BOB, json=answer)).status_code == 404
        assert (await client.post(
            path + "/answers", headers=ALICE, json={**answer, "answer": "add_panel"},
        )).status_code == 422
        assert (await client.post(
            path + "/answers", headers=ALICE, json={**answer, "reason": " "},
        )).status_code == 422
        replies = await asyncio.gather(*[
            client.post(path + "/answers", headers=ALICE, json=answer) for _ in range(2)
        ])
        assert sorted(reply.status_code for reply in replies) == [200, 409]
        result = success(await client.get(path, headers=ALICE))
        assert result["status"] == "ready_for_allocation" and result["report"]["classified_total"] == "100.00"
        assert result["report"]["supplementary_rules"] == draft["report"]["supplementary_rules"]
        assert result["report"]["rule_context"] == draft["report"]["rule_context"]
        assert result["decisions"][question["key"]]["operator"] == "alice"
        assert result["decisions"][question["key"]]["evidence"] == question["evidence"]
        panel, _ = result["report"]["assignments"]
        assert (panel["scope_origin"], panel["amount"]) == ("in_source", "12")
        success(await client.put(
            f"/tasks/{task_id}/target", headers=ALICE, json={
                **START, "target": {"amount": "100.00", "gst_basis": "excluding_gst"},
            },
        ))
        assert (await client.post(path + "/answers", headers=ALICE, json=answer)).status_code == 409
        assert (await client.get(path, headers=ALICE)).status_code == 409


@pytest.mark.anyio
async def test_scope_change_answer_waits_for_reconciliation(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    async def model(_settings, _spec, inputs):
        return model_answer(inputs, {f"{SHEET}!D4": "Access Panel"})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({
        WORKSHEET: source_sheet([
            ("Access Panel", "12", None), ("Temporary fencing", "88", None),
            ("Project Total ex GST", "100", "SUM(D4:D5)"),
        ]),
        "xl/worksheets/sheet2.xml": f"""
<worksheet xmlns="{SHEET_NS}"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Exclusions</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>Access Panels</t></is></c></row>
</sheetData></worksheet>""".encode(),
    })
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, original = await ready_task(client, source)
        path = f"/tasks/{task_id}/classification"
        draft = success(await client.post(path, headers=ALICE, json=START))
        question = draft["report"]["questions"][0]
        assert question["evidence"][-1] == "Notes!A2"
        answer = {
            **START, "question_id": question["id"], "answer": "change_scope",
            "reason": "The allowance belongs outside the current authorized scope.",
        }
        revised = success(await client.post(path + "/answers", headers=ALICE, json=answer))
        assert revised["status"] == "blocked" and revised["report"]["questions"][0]["kind"] == "reconcile_scope"
        reconciliation_path = f"/tasks/{task_id}/reconciliation"
        assert success(await client.get(reconciliation_path, headers=ALICE)) == original
        updated = success(await client.post(reconciliation_path, headers=ALICE, json={
            **START, "scope": {"exclude": [f"{SHEET}!D4"]},
        }))
        assert updated["generation"] != original["generation"]
        assert (await client.get(path, headers=ALICE)).status_code == 404
        assert (await client.post(path + "/answers", headers=ALICE, json=answer)).status_code == 409
        result = success(await client.post(path, headers=ALICE, json=START))
        assert result["report"]["classified_total"] == "88.00" and result["decisions"] == {}
        assert [line["row"] for line in result["report"]["assignments"]] == [f"{SHEET}!D5"]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["same_revision_scope", "new_source"])
async def test_classification_rejects_stale_publication(
    task_runtime, configured_skill, monkeypatch, workbook_with, change,
):
    started, publish = asyncio.Event(), asyncio.Event()

    async def model(_settings, _spec, inputs):
        started.set()
        await publish.wait()
        return model_answer(inputs, {})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({WORKSHEET: source_sheet([
        ("Temporary fencing", "100", None), ("Hoarding", "20", None),
        ("Project Total ex GST", "120", "SUM(D4:D5)"),
    ])})
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, original = await ready_task(client, source)
        path = f"/tasks/{task_id}/classification"
        pending = asyncio.create_task(client.post(path, headers=ALICE, json=START))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if change == "same_revision_scope":
                rec = f"/tasks/{task_id}/reconciliation"
                success(await client.post(rec, headers=ALICE, json={**START, "scope": {"exclude": [f"{SHEET}!D5"]}}))
                restored = success(await client.post(rec, headers=ALICE, json={**START, "scope": {}}))
                assert original["report"] == restored["report"]
                assert original["generation"] != restored["generation"]
            else:
                replacement = workbook_with({WORKSHEET: source_sheet([
                    ("Replacement", "90", None), ("Project Total ex GST", "90", "D4"),
                ])})
                success(await client.put(
                    f"/tasks/{task_id}/source", headers=ALICE, content=replacement,
                    params={"expected_revision": 2, "filename": "replacement.xlsx"},
                ))
        finally:
            publish.set()
            response = await pending
        assert response.status_code == 409
        assert (await client.get(path, headers=ALICE)).status_code == (404 if change == "same_revision_scope" else 409)


@pytest.mark.anyio
async def test_model_failure_is_persisted_and_retryable(task_runtime, configured_skill, monkeypatch, workbook_with):
    client_class = httpx.AsyncClient

    def malformed(_request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": None}]})

    monkeypatch.setattr(settings, "llm_api_key", SecretStr("example-key"))
    monkeypatch.setattr(classification_model.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(malformed), **kwargs,
    ))
    source = workbook_with({WORKSHEET: source_sheet([
        ("Temporary hoarding", "100", None), ("Project Total ex GST", "100", "D4"),
    ])})
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, source)
        path = f"/tasks/{task_id}/classification"
        assert (await client.post(path, headers=ALICE, json=START)).status_code == 503
        failed_record = success(await client.get(path, headers=ALICE))
        assert failed_record["status"] == "failed"
        assert failed_record["report"]["rule_context"]["primary_verified"] is True
        assert {name: value for name, value in failed_record["report"].items() if name != "rule_context"} == {
            "status": "failed", "error": "invalid_model_proposal", "questions": [], "assignments": [],
            "supplementary_rules": {"status": "not_configured", "version": None, "sha256": None, "sections": []},
        }

    async def available(_settings, _spec, inputs):
        return model_answer(inputs, {})

    monkeypatch.setattr(classification_model, "propose", available)
    async with service() as client:
        result = success(await client.post(path, headers=ALICE, json=START))
        assert result["status"] == "ready_for_allocation" and result["report"]["classified_total"] == "100.00"
        assert success(await client.post(path, headers=ALICE, json=START)) == result


@pytest.mark.anyio
async def test_corrected_doors_trade_clears_false_panel_scope_conflict(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    async def model(_settings, _spec, inputs):
        return model_answer(inputs, {f"{SHEET}!D4": "Access Panel"})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({
        WORKSHEET: source_sheet([
            ("FF&E", "40", None), ("Project Total ex GST", "40", "D4"),
        ]),
        "xl/worksheets/sheet2.xml": f"""
<worksheet xmlns="{SHEET_NS}"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Access Panels excluded.</t></is></c></row>
</sheetData></worksheet>""".encode(),
    })
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, source)
        path = f"/tasks/{task_id}/classification"
        draft = success(await client.post(path, headers=ALICE, json=START))
        question, = draft["report"]["questions"]
        assert question["kind"] == "trade"
        ready = success(await client.post(path + "/answers", headers=ALICE, json={
            **START, "question_id": question["id"], "answer": "Doors", "reason": "The FF&E item belongs to Doors.",
        }))
        assert ready["status"] == "ready_for_allocation"
        assert ready["report"]["questions"] == []
        item, = ready["report"]["assignments"]
        assert (item["trade"], item["amount"], item["trade_origin"]) == ("Doors", "40", "operator_confirmed")


@pytest.mark.anyio
async def test_new_classifier_version_replaces_prior_ready_result(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    calls = []

    async def model(_settings, _spec, inputs):
        calls.append(True)
        return model_answer(inputs, {})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({WORKSHEET: source_sheet([
        ("Hoarding", "25", None), ("Project Total ex GST", "25", "D4"),
    ])})
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, source)
        path = f"/tasks/{task_id}/classification"
        with monkeypatch.context() as old_classifier:
            old_classifier.setattr(classifications, "CLASSIFIER_VERSION", "2")
            old = success(await client.post(path, headers=ALICE, json=START))
            assert old["status"] == "ready_for_allocation" and old["classifier_version"] == "2"
        assert (await client.get(path, headers=ALICE)).status_code == 404
        current = success(await client.post(path, headers=ALICE, json=START))
        assert current["classifier_version"] == classifications.CLASSIFIER_VERSION
        assert current["status"] == "ready_for_allocation" and len(calls) == 2


@pytest.mark.anyio
async def test_owner_must_confirm_combined_floor_before_classification_advances(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    combined = "Level 1 and Level 2"

    async def model(_settings, _spec, inputs):
        proposal = model_answer(inputs, {})
        proposal.rows[0].area = combined
        return proposal

    monkeypatch.setattr(classification_model, "propose", model)
    workbook = component_workbook(workbook_with, area=combined, parent="Hoarding")
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, workbook)
        path = f"/tasks/{task_id}/classification"
        draft = success(await client.post(path, headers=ALICE, json=START))
        question, = draft["report"]["questions"]
        assert (question["kind"], question["options"]) == ("area", [combined])
        assert draft["status"] == "blocked"
        assert draft["report"]["classified_total"] is None
        assert draft["report"]["assignments"] == []
        answer = {**START, "question_id": question["id"], "answer": combined}

    async with service() as client:
        assert success(await client.get(path, headers=ALICE)) == draft
        assert (await client.post(path + "/answers", headers=BOB, json=answer)).status_code == 404
        assert (await client.post(
            path + "/answers", headers=ALICE, json={**answer, "answer": "Level 1"},
        )).status_code == 422
        ready = success(await client.post(path + "/answers", headers=ALICE, json=answer))
        assert ready["status"] == "ready_for_allocation"
        assert ready["report"]["classified_total"] == ready["report"]["working_scope_total"] == "110.00"
        assignment, = ready["report"]["assignments"]
        assert (assignment["area"], assignment["area_origin"]) == (combined, "operator_confirmed")
