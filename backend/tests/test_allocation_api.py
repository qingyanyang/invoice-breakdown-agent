import asyncio
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from app import classification_model
from app.checkpoints import setup_checkpoints
from app.config import settings
from test_classification_api import START, configured_skill, model_answer
from test_reconciliation import SHEET_NS, WORKSHEET, source_sheet
from test_reconciliation_api import ALICE, BOB, FIXTURES, create_source, establish_evidence, service, success

SHEET = "Trade Summary"
ADDITION = f"{SHEET}!D13"
TRADE_BY_ROW = {
    f"{SHEET}!D4": "Partition", f"{SHEET}!D5": "Glazing", f"{SHEET}!D7": "Doors",
    f"{SHEET}!D8": "Doors", f"{SHEET}!D10": "Make Good",
    f"{SHEET}!D11": "Access Panel", ADDITION: "Access Panel",
}


async def fixture_model(_settings, _spec, inputs):
    return model_answer(inputs, TRADE_BY_ROW)


def add_authorized_option(workbook_with):
    with ZipFile(FIXTURES / "reconciliation.xlsx") as original:
        worksheet = ElementTree.fromstring(original.read(WORKSHEET))
    sheet_data = worksheet.find(f"{{{SHEET_NS}}}sheetData")
    assert sheet_data is not None
    sheet_data.append(ElementTree.fromstring(f"""
<row xmlns="{SHEET_NS}" r="13">
<c r="A13" t="inlineStr"><is><t>Option - Feature Access Panel (ex GST)</t></is></c>
<c r="D13"><v>1.01</v></c>
</row>"""))
    return workbook_with({WORKSHEET: ElementTree.tostring(worksheet)})


async def reconcile_and_classify(client, task, scope=None):
    reconcile_request = {**START, "scope": scope}
    reconciled = success(await client.post(f"{task}/reconciliation", headers=ALICE, json=reconcile_request))
    assert reconciled["status"] == "ready_for_classification"
    classified = success(await client.post(f"{task}/classification", headers=ALICE, json=START))
    assert classified["status"] == "ready_for_allocation", classified["report"]["questions"]
    return classified


@pytest.mark.anyio
async def test_allocation_uses_persisted_gst_approval_and_invalidates_stale_inputs(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id = success(await client.post("/tasks", headers=ALICE, json={
            "target": {"amount": "282.54", "gst_basis": "unknown"},
        }), 201)["id"]
        task = f"/tasks/{task_id}"
        path = task + "/allocation"
        source = add_authorized_option(workbook_with)
        success(await client.put(
            task + "/source", headers=ALICE, content=source,
            params={"expected_revision": 1, "filename": "fixture.xlsx"},
        ))
        started = success(await client.post(task + "/workflow", headers=ALICE, json=START))
        current_question = started["question"]["id"]
        pending = await client.post(path, headers=ALICE, json={**START, "expected_classification_id": str(uuid4())})
        assert pending.status_code == 409 and "GST basis" in pending.json()["detail"]
        success(await client.post(task + "/workflow/answers", headers=ALICE, json={
            **START, "question_id": current_question, "answer": "ex_gst",
        }))
        success(await client.post(task + "/extraction", headers=ALICE, json=START))
        classified = await reconcile_and_classify(client, task, {"include": [ADDITION]})
        request = {**START, "expected_classification_id": classified["proposal_id"]}
        assert (await client.post(path, headers=BOB, json=request)).status_code == 404
        result = success(await client.post(path, headers=ALICE, json=request))
        report = result["report"]
        assert result["classification_id"] == classified["proposal_id"]
        assert report["target_gst_confirmation_id"] == current_question
        assert report["source_sha256"] == classified["source_sha256"]
        assert report["working_total"] == "261.01"
        assert report["protected_total"] == "46.01"
        assert report["display"]["total"] == "282.54"
        assert report["display"]["rounding"]["difference"] == "0.00"
        assert report["display"]["rounding"]["residual_applied"] == "-0.01"
        assert success(await client.post(path, headers=ALICE, json=request)) == result

    async with service() as client:
        assert success(await client.get(path, headers=ALICE)) == result
        assert (await client.get(path, headers=BOB)).status_code == 404
        assert (await client.get(task + "/source", headers=ALICE)).content == source

        revised = await reconcile_and_classify(
            client, task, {"include": [ADDITION], "exclude": [f"{SHEET}!D8"]},
        )
        assert revised["proposal_id"] != classified["proposal_id"]
        assert (await client.get(path, headers=ALICE)).status_code == 404
        stale = await client.post(path, headers=ALICE, json=request)
        assert stale.status_code == 409 and "classification has changed" in stale.json()["detail"]
        success(await client.put(task + "/target", headers=ALICE, json={
            **START, "target": {"amount": "290.00", "gst_basis": "excluding_gst"},
        }))
        assert (await client.post(path, headers=ALICE, json=request)).status_code == 409


@pytest.mark.anyio
async def test_allocation_policy_updates_are_atomic(task_runtime, configured_skill, monkeypatch, workbook_with):
    async def model(_settings, _spec, inputs):
        return model_answer(inputs, {})

    monkeypatch.setattr(classification_model, "propose", model)
    await setup_checkpoints(settings)
    workbook = workbook_with({WORKSHEET: source_sheet([
        ("Net miscellaneous work", "20", None), ("Project Total ex GST", "20", "D4"),
    ])})
    async with service() as client:
        task_id = await create_source(client, "source.xlsx", workbook)
        task = f"/tasks/{task_id}"
        path = task + "/allocation"
        await establish_evidence(client, task_id)
        classified = await reconcile_and_classify(client, task)
        base = {**START, "expected_classification_id": classified["proposal_id"]}
        result = success(await client.post(path, headers=ALICE, json=base))
        assert result["report"]["mode"] == "no_contract_price_adjustment"
        assert result["report"]["factor"] is None
        before = {**base, "expected_allocation_id": result["allocation_id"]}
        tasks = [client.post(path, headers=ALICE, json={
            **before, "policy": {"requested_direction": direction},
        }) for direction in ("increase", "decrease")]
        replies = await asyncio.gather(*tasks)
        assert sorted(response.status_code for response in replies) == [200, 409]
        updated = next(response.json() for response in replies if response.status_code == 200)
        assert updated["allocation_id"] != result["allocation_id"]
        assert success(await client.get(path, headers=ALICE)) == updated
        repeat = {**before, "policy": updated["policy"]}
        assert success(await client.post(path, headers=ALICE, json=repeat)) == updated
