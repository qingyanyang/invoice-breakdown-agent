import asyncio
from decimal import Decimal
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from app.features.classification import model as classification_model
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


def allocation_workbook(workbook_with, rows):
    source_rows = [(description, amount, None) for description, amount, _ in rows]
    total = sum(Decimal(amount) for _, amount, _ in rows)
    source_rows.append(("Project Total ex GST", str(total), f"SUM(D4:D{3 + len(rows)})"))
    worksheet = ElementTree.fromstring(source_sheet(source_rows))
    row_cells = worksheet.findall(f"{{{SHEET_NS}}}sheetData/{{{SHEET_NS}}}row")
    for row, label in zip(row_cells, ["Area", *[area for _, _, area in rows]]):
        coordinate = "E" + row.attrib["r"]
        cell = ElementTree.SubElement(row, f"{{{SHEET_NS}}}c", r=coordinate, t="inlineStr")
        inline = ElementTree.SubElement(cell, f"{{{SHEET_NS}}}is")
        ElementTree.SubElement(inline, f"{{{SHEET_NS}}}t").text = label
    return workbook_with({WORKSHEET: ElementTree.tostring(worksheet)})


async def approve_target(client, workbook, target):
    task_id = success(await client.post("/tasks", headers=ALICE, json={
        "target": {"amount": target, "gst_basis": "excluding_gst"},
    }), 201)["id"]
    task = f"/tasks/{task_id}"
    success(await client.put(
        task + "/source", headers=ALICE, content=workbook,
        params={"expected_revision": 1, "filename": "allocation.xlsx"},
    ))
    await establish_evidence(client, task_id)
    classified = await reconcile_and_classify(client, task)
    return task + "/allocation", {**START, "expected_classification_id": classified["proposal_id"]}


async def protected_first_model(_settings, _spec, inputs):
    return model_answer(inputs, {f"{SHEET}!D4": "Make Good"})


@pytest.mark.anyio
async def test_api_persists_blocked_amplification_of_signed_scope(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    async def model(_settings, _spec, inputs):
        return model_answer(inputs, {})

    monkeypatch.setattr(classification_model, "propose", model)
    await setup_checkpoints(settings)
    workbook = allocation_workbook(workbook_with, [
        ("Miscellaneous work", "100000.00", "Area A"),
        ("Miscellaneous credit", "-99999.99", "Area B"),
    ])
    async with service() as client:
        path, request = await approve_target(client, workbook, "10000.00")
        result = success(await client.post(path, headers=ALICE, json=request))
        assert result["status"] == "blocked"
        assert result["report"]["issue"]["code"] == "implausible_factor"
        assert "display" not in result["report"]
        assert result["report"]["issue"]["evidence"]["proposed_factor"] == "1000000"
    async with service() as client:
        assert success(await client.get(path, headers=ALICE)) == result


@pytest.mark.anyio
async def test_api_publishes_distributed_residual_and_protects_amounts(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", protected_first_model)
    await setup_checkpoints(settings)
    rows = [("Make Good", "100.00", "Protected")]
    rows.extend(("Miscellaneous work", "0.01", f"Area {index:02}") for index in range(1, 11))
    async with service() as client:
        path, request = await approve_target(client, allocation_workbook(workbook_with, rows), "100.05")
        result = success(await client.post(path, headers=ALICE, json=request))
        assert result["status"] == "ready_for_workbook"
        display = result["report"]["display"]
        assert display["total"] == "100.05"
        assert display["trade_totals"]["Make Good"] == "100.00"
        assert display["rounding"]["recipients"] == [
            {"area": f"Area {index:02}", "trade": "Other", "amount": "-0.01"} for index in range(1, 6)
        ]


@pytest.mark.anyio
async def test_api_zero_residual_stays_ready_after_preference_update(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", protected_first_model)
    await setup_checkpoints(settings)
    workbook = allocation_workbook(workbook_with, [
        ("Make Good", "10.00", "Area A"), ("Miscellaneous work", "1.00", "Area B"),
    ])
    async with service() as client:
        path, request = await approve_target(client, workbook, "10.00")
        original = success(await client.post(path, headers=ALICE, json=request))
        assert original["status"] == "ready_for_workbook"
        preference = {"area": "Area B", "trade": "Other"}
        updated = success(await client.post(path, headers=ALICE, json={
            **request, "expected_allocation_id": original["allocation_id"],
            "policy": {"rounding_preference": preference},
        }))
        assert updated["status"] == "ready_for_workbook"
        assert updated["policy"]["rounding_preference"] == preference
        assert updated["report"] == original["report"]
