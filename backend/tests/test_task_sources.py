import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from app import main, source_cleanup
from app import tasks as task_api
from app.source_storage import LocalSourceStorage, StorageError

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "invoice_breakdown"
ALICE = {"Authorization": "Bearer " + "a" * 40}
BOB = {"Authorization": "Bearer " + "b" * 40}


@asynccontextmanager
async def running_service():
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        yield client


async def create_task(client, payload=None):
    response = await client.post("/tasks", json=payload or {}, headers=ALICE)
    assert response.status_code == 201, response.text
    return response.json()


async def upload(client, task_id, data, revision=1, headers=ALICE, filename="sample.xlsx"):
    return await client.put(
        f"/tasks/{task_id}/source",
        params={"expected_revision": revision, "filename": filename},
        content=data, headers=headers,
    )


@pytest.mark.anyio
async def test_persist_source_and_target_revisions_across_restart(task_runtime):
    original = (FIXTURES / "reconciliation.xlsx").read_bytes()
    replacement = (FIXTURES / "scope-ambiguity.xlsx").read_bytes()
    async with running_service() as client:
        assert (await client.post("/tasks", json={})).status_code == 401
        assert (await client.post("/tasks", json={"owner": "bob"}, headers=ALICE)).status_code == 422
        assert (await client.post("/tasks", json={
            "target": {"amount": "25.10"},
        }, headers=ALICE)).status_code == 422
        task = await create_task(client, {"target": {"amount": "25.10", "gst_basis": "unknown"}})
        task_id = task["id"]
        assert task["target"] == {"amount": "25.10", "gst_basis": "unknown"}
        assert task["status"] == "awaiting_source"

        assert (await client.get(f"/tasks/{task_id}", headers=BOB)).status_code == 404
        assert (await upload(client, task_id, original, headers=BOB)).status_code == 404
        response = await upload(client, task_id, original)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 2
        assert response.json()["source"]["sha256"] == sha256(original).hexdigest()
        assert (await upload(client, task_id, replacement, revision=1)).status_code == 409
        assert (await client.get(f"/tasks/{task_id}/source?revision=2", headers=BOB)).status_code == 404

        target = {"expected_revision": 2, "target": {"amount": "125.11", "gst_basis": "including_gst"}}
        assert (await client.put(f"/tasks/{task_id}/target", json=target, headers=BOB)).status_code == 404
        response = await client.put(f"/tasks/{task_id}/target", json=target, headers=ALICE)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 3
        assert (await client.put(f"/tasks/{task_id}/target", json=target, headers=ALICE)).status_code == 409

    async with running_service() as client:
        task = (await client.get(f"/tasks/{task_id}", headers=ALICE)).json()
        assert task["revision"] == 3
        assert task["target"] == {"amount": "125.11", "gst_basis": "including_gst"}
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == original
        response = await upload(client, task_id, replacement, revision=3)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 4
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == replacement
        assert (await client.get(f"/tasks/{task_id}/source?revision=2", headers=ALICE)).content == original
    assert len(list(task_runtime.rglob("*.xlsx"))) == 2


@pytest.mark.anyio
async def test_same_revision_can_be_uploaded_only_once(task_runtime):
    workbook = (FIXTURES / "reconciliation.xlsx").read_bytes()
    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        first, second = await asyncio.gather(
            upload(client, task_id, workbook), upload(client, task_id, workbook),
        )
        assert sorted((first.status_code, second.status_code)) == [200, 409]
        assert (await client.get(f"/tasks/{task_id}", headers=ALICE)).json()["revision"] == 2
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == workbook
    assert len(list(task_runtime.rglob("*.xlsx"))) == 1


@pytest.mark.anyio
async def test_invalid_workbooks_and_failed_writes_leave_no_new_revision(task_runtime, monkeypatch):
    workbook = (FIXTURES / "reconciliation.xlsx").read_bytes()
    original_put = LocalSourceStorage.put

    def fail_after_storage(self, key, data):
        original_put(self, key, data)
        raise StorageError("private storage error")

    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        assert (await upload(client, task_id, workbook, filename="legacy.xls")).status_code == 415
        assert (await upload(client, task_id, b"not a workbook")).status_code == 422
        with monkeypatch.context() as patch:
            patch.setattr(LocalSourceStorage, "put", fail_after_storage)
            response = await upload(client, task_id, workbook)
            assert response.status_code == 503
            assert "private storage error" not in response.text
        original_append = task_api.append_revision

        async def fail_before_commit(*args):
            await original_append(*args)
            raise SQLAlchemyError("private database error")

        with monkeypatch.context() as patch:
            patch.setattr(task_api, "append_revision", fail_before_commit)
            response = await upload(client, task_id, workbook)
            assert response.status_code == 503
            assert "private database error" not in response.text
        assert not list(task_runtime.rglob("*.xlsx"))
        assert (await client.get(f"/tasks/{task_id}", headers=ALICE)).json()["revision"] == 1
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).status_code == 404
        assert (await upload(client, task_id, workbook)).status_code == 200


@pytest.mark.anyio
async def test_uncertain_commit_keeps_referenced_source(task_runtime, monkeypatch):
    workbook = (FIXTURES / "reconciliation.xlsx").read_bytes()
    original_transaction = task_api.transaction
    transactions = 0

    @asynccontextmanager
    async def commit_then_lose_response(engine):
        nonlocal transactions
        transactions += 1
        transaction_number = transactions
        async with original_transaction(engine) as connection:
            yield connection
        if transaction_number == 2:
            raise HTTPException(503, "Task database unavailable")

    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        with monkeypatch.context() as patch:
            patch.setattr(task_api, "transaction", commit_then_lose_response)
            assert (await upload(client, task_id, workbook)).status_code == 503
        assert (await client.get(f"/tasks/{task_id}", headers=ALICE)).json()["revision"] == 2
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == workbook
        file, = task_runtime.rglob("*.xlsx")
        key = file.relative_to(task_runtime).as_posix()
        assert not await task_api.discard_unreferenced(
            main.app.state.database_engine, main.app.state.source_storage, UUID(task_id), key,
        )
        assert file.read_bytes() == workbook


@pytest.mark.anyio
@pytest.mark.parametrize(
    "replacement",
    [
        b'<unrelated xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        b'<?xml version="1.0" encoding="invalid-test-encoding"?><worksheet/>',
    ],
)
async def test_invalid_sheet_leaves_task_unchanged(task_runtime, workbook_with, replacement):
    workbook = workbook_with({"xl/worksheets/sheet1.xml": replacement})
    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        response = await upload(client, task_id, workbook)
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Invalid or malformed .xlsx workbook"}
        task = (await client.get(f"/tasks/{task_id}", headers=ALICE)).json()
        assert task["revision"] == 1
        assert task["status"] == "awaiting_source"
    assert not list(task_runtime.rglob("*.xlsx"))


@pytest.mark.anyio
async def test_staging_cleanup_failure_still_publishes_and_can_be_recovered(
    task_runtime, monkeypatch, caplog,
):
    workbook = (FIXTURES / "reconciliation.xlsx").read_bytes()
    original_unlink = Path.unlink

    def fail_staging_unlink(path, *args, **kwargs):
        if path.suffix == ".staging":
            raise PermissionError("private staging path")
        original_unlink(path, *args, **kwargs)

    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_staging_unlink)
            response = await upload(client, task_id, workbook)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 2
        staged, = task_runtime.rglob("*.staging")
        key = staged.relative_to(task_runtime).as_posix()
        assert f"Source staging cleanup pending for key {key}" in caplog.text
        assert "private staging path" not in caplog.text
        assert await source_cleanup.cleanup(key)
        assert not staged.exists()
        assert (await client.get(f"/tasks/{task_id}/source", headers=ALICE)).content == workbook
