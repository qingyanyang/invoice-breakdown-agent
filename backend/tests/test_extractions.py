import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import extractions, main
from app.checkpoints import setup_checkpoints
from app.config import settings
from app.extraction_reader import EXTRACTOR_VERSION

ALICE = {"Authorization": "Bearer " + "a" * 40}
BOB = {"Authorization": "Bearer " + "b" * 40}
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@asynccontextmanager
async def running_service():
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        yield client


def success(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


async def upload(client, task_id, workbook, revision):
    return success(await client.put(
        f"/tasks/{task_id}/source", content=workbook, headers=ALICE,
        params={"expected_revision": revision, "filename": "sample.xlsx"},
    ))


async def create_ready_task(client, workbook, *, start=True):
    task_id = success(await client.post("/tasks", json={}, headers=ALICE), 201)["id"]
    assert (await upload(client, task_id, workbook, 1))["revision"] == 2
    if start:
        payload = success(await client.post(
            f"/tasks/{task_id}/workflow", json={"expected_revision": 2}, headers=ALICE,
        ))
        assert payload["status"] == "ready_for_extraction"
    return task_id


@pytest.mark.anyio
async def test_extraction_is_owned_versioned_and_persistent(task_runtime, workbook_with):
    workbook = workbook_with({})
    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id = await create_ready_task(client, workbook, start=False)
        path = f"/tasks/{task_id}/extraction"
        assert (await client.post(path, json={"expected_revision": 2}, headers=ALICE)).status_code == 409
        success(await client.post(
            f"/tasks/{task_id}/workflow", json={"expected_revision": 2}, headers=ALICE,
        ))
        assert (await client.post(path, json={"expected_revision": 2}, headers=BOB)).status_code == 404
        result = success(await client.post(path, json={"expected_revision": 2}, headers=ALICE))
        assert result["extractor_version"] == EXTRACTOR_VERSION
        assert result["source_sha256"] == sha256(workbook).hexdigest()
        assert result["status"] == "extracted" and result["revision"] == 2
        assert result["result"]["raw"]["sheets"][0]["name"] == "Trade Summary"
    async with running_service() as client:
        assert (await client.get(path, headers=BOB)).status_code == 404
        assert success(await client.get(path, headers=ALICE)) == result
        assert success(await client.post(path, json={"expected_revision": 2}, headers=ALICE)) == result
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == workbook


@pytest.mark.anyio
async def test_parser_failure_records_no_partial_result(task_runtime, workbook_with):
    workbook = workbook_with({"xl/worksheets/sheet1.xml": f"""
<worksheet xmlns="{SHEET_NS}">
  <sheetData><row r="1"><c r="A1" t="s"><v>999999</v></c></row></sheetData>
</worksheet>
""".encode()})
    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id = await create_ready_task(client, workbook)
        path = f"/tasks/{task_id}/extraction"
        assert (await client.post(path, json={"expected_revision": 2}, headers=ALICE)).status_code == 422
        record = success(await client.get(path, headers=ALICE))
        assert record["status"] == "failed" and record["error_code"] == "invalid_workbook"
        assert record["result"] is None


@pytest.mark.anyio
async def test_execution_deadline_is_recorded(task_runtime, workbook_with, monkeypatch):
    await setup_checkpoints(settings)

    async def stalled_process(*args, cancellable):
        assert cancellable
        await asyncio.sleep(5)
        pytest.fail("Extraction deadline did not cancel the process wait")

    monkeypatch.setattr(extractions.to_process, "run_sync", stalled_process)
    monkeypatch.setattr(extractions, "EXTRACTION_TIMEOUT_SECONDS", 0.1)
    async with running_service() as client:
        task_id = await create_ready_task(client, workbook_with({}))
        path = f"/tasks/{task_id}/extraction"
        assert (await client.post(path, json={"expected_revision": 2}, headers=ALICE)).status_code == 504
        record = success(await client.get(path, headers=ALICE))
        assert record["status"] == "failed" and record["error_code"] == "deadline_exceeded"
        assert record["result"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["replace_source", "alter_checksum"])
async def test_changed_source_cannot_publish_inflight_result(
    task_runtime, disposable_postgres, workbook_with, monkeypatch, mutation,
):
    admin, schema = disposable_postgres
    workbook = workbook_with({})
    await setup_checkpoints(settings)
    extracted = asyncio.Event()
    proceed = asyncio.Event()
    original_extract = extractions.extract_source

    async def pause_before_publish(*args):
        result = await original_extract(*args)
        extracted.set()
        await proceed.wait()
        return result

    monkeypatch.setattr(extractions, "extract_source", pause_before_publish)
    async with running_service() as client:
        task_id = await create_ready_task(client, workbook)
        path = f"/tasks/{task_id}/extraction"
        request = asyncio.create_task(client.post(path, json={"expected_revision": 2}, headers=ALICE))
        try:
            await asyncio.wait_for(extracted.wait(), timeout=10)
            if mutation == "replace_source":
                assert (await upload(client, task_id, workbook, 2))["revision"] == 3
            else:
                with admin.begin() as connection:
                    connection.execute(text(
                        f'UPDATE "{schema}".task_revisions SET source_sha256 = :sha '
                        "WHERE task_id = :task AND revision = 2"
                    ), {"task": task_id, "sha": "0" * 64})
        finally:
            proceed.set()
        assert (await request).status_code == 409
        assert (await client.get(path, headers=ALICE)).status_code == 404
    with admin.connect() as connection:
        assert connection.scalar(text(f'SELECT count(*) FROM "{schema}".source_extractions')) == 0
