import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.checkpoints import setup_checkpoints
from app.config import settings

ALICE = {"Authorization": "Bearer " + "a" * 40}
BOB = {"Authorization": "Bearer " + "b" * 40}
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "invoice_breakdown"


@asynccontextmanager
async def service():
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        yield client


def success(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


async def create_source(client, filename, workbook=None):
    task = success(await client.post("/tasks", headers=ALICE, json={}), 201)
    task_id = task["id"]
    source = success(await client.put(
        f"/tasks/{task_id}/source",
        params={"expected_revision": 1, "filename": filename},
        content=workbook if workbook is not None else (FIXTURES / filename).read_bytes(),
        headers=ALICE,
    ))
    assert source["revision"] == 2
    return task_id


async def establish_evidence(client, task_id):
    for step in ("workflow", "extraction"):
        success(await client.post(
            f"/tasks/{task_id}/{step}", headers=ALICE, json={"expected_revision": 2},
        ))


@pytest.mark.anyio
async def test_reconciliation_persists_answers_and_requires_current_revision(task_runtime):
    await setup_checkpoints(settings)
    async with service() as client:
        task_id = await create_source(client, "scope-ambiguity.xlsx")
        path = f"/tasks/{task_id}/reconciliation"
        payload = {"expected_revision": 2, "scope": {"mode": "base_only"}}
        assert (await client.post(path, headers=ALICE, json=payload)).status_code == 409
        await establish_evidence(client, task_id)
        assert (await client.post(path, headers=BOB, json=payload)).status_code == 404
        record = success(await client.post(path, headers=ALICE, json=payload))
        assert record["status"] == "blocked"
        first, second = record["report"]["questions"]
        answer = {"expected_revision": 2, "question_id": first["id"], "answer": "included"}
        assert (await client.post(path + "/answers", headers=BOB, json=answer)).status_code == 404
        assert (await client.post(
            path + "/answers", headers=ALICE, json={**answer, "answer": "75"},
        )).status_code == 422
        replies = await asyncio.gather(*[
            client.post(path + "/answers", headers=ALICE, json=answer) for _ in range(2)
        ])
        assert sorted(reply.status_code for reply in replies) == [200, 409]

    async with service() as client:
        stored = success(await client.get(path, headers=ALICE))
        assert stored["status"] == "blocked"
        assert stored["decisions"][first["key"]]["answer"] == "included"
        assert stored["decisions"][first["key"]]["evidence"] == first["evidence"]
        assert (await client.get(path, headers=BOB)).status_code == 404
        ready = success(await client.post(
            path + "/answers", headers=ALICE, json={**answer, "question_id": second["id"]},
        ))
        assert ready["status"] == "ready_for_classification"
        assert ready["report"]["working_scope_total"] == "75.00"
        assert ready["report"]["difference"] == "0.00"
        assert [item["effect"] for item in ready["report"]["scope_exclusions"]] == ["-15", "15"]
        revision = success(await client.put(
            f"/tasks/{task_id}/target", headers=ALICE,
            json={"expected_revision": 2, "target": {"amount": "100.00", "gst_basis": "excluding_gst"}},
        ))
        assert revision["revision"] == 3
        assert (await client.post(path + "/answers", headers=ALICE, json=answer)).status_code == 409
        assert (await client.get(path, headers=ALICE)).status_code == 409


@pytest.mark.anyio
async def test_reconciliation_publishes_a_verified_control_with_exact_source(task_runtime):
    await setup_checkpoints(settings)
    async with service() as client:
        task_id = await create_source(client, "reconciliation.xlsx")
        await establish_evidence(client, task_id)
        record = success(await client.post(
            f"/tasks/{task_id}/reconciliation", headers=ALICE, json={"expected_revision": 2},
        ))
        assert record["status"] == "ready_for_classification"
        current = success(await client.get(f"/tasks/{task_id}/extraction", headers=ALICE))
        assert (record["extractor_version"], record["source_sha256"]) == (
            current["extractor_version"], current["source_sha256"],
        )
        assert len(record["report"]["selected_rows"]) == 6
        assert record["report"]["working_scope_total"] == "260.00"


@pytest.mark.anyio
async def test_material_difference_requires_corrected_workbook(task_runtime, workbook_with):
    await setup_checkpoints(settings)
    source = workbook_with({"xl/worksheets/sheet1.xml": b"""
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="3"><c r="A3" t="inlineStr"><is><t>Description</t></is></c>
<c r="D3" t="inlineStr"><is><t>Amount</t></is></c></row>
<row r="4"><c r="A4" t="inlineStr"><is><t>Base work</t></is></c><c r="D4"><v>125</v></c></row>
<row r="5"><c r="A5" t="inlineStr"><is><t>Project Total ex GST</t></is></c>
<c r="D5"><f>D4</f><v>100</v></c></row>
</sheetData></worksheet>"""})
    async with service() as client:
        task_id = await create_source(client, "mismatched.xlsx", source)
        await establish_evidence(client, task_id)
        path = f"/tasks/{task_id}/reconciliation"
        record = success(await client.post(path, headers=ALICE, json={"expected_revision": 2}))
        report = record["report"]
        assert record["status"] == "blocked" and report["difference"] == "25.00"
        assert report["working_scope_total"] is None
        question, = report["questions"]
        assert question["response_type"] == "source_correction"
        assert question["options"] == []
        assert "Trade Summary!D5" in question["prompt"]
        assert question["evidence"] == [{"sheet": "Trade Summary", "cell": "D5"}]
        assert "detail and subtotal evidence" in question["required_evidence"]
        answer = await client.post(path + "/answers", headers=ALICE, json={
            "expected_revision": 2, "question_id": question["id"], "answer": "confirmed",
        })
        assert answer.status_code == 422
        assert "corrected source evidence" in answer.json()["detail"]
        assert success(await client.get(path, headers=ALICE)) == record
