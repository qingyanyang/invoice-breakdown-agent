import asyncio
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy.exc import SQLAlchemyError

from app import classification_model, main, workbooks
from app.checkpoints import setup_checkpoints
from app.config import settings
from app.source_storage import StorageError
from test_allocation_api import allocation_workbook, approve_target, protected_first_model
from test_classification_api import START, configured_skill, model_answer
from test_reconciliation_api import ALICE, BOB, service, success
from test_workbook_generation import unsupported_drawing


async def misc_model(_settings, _specification, inputs):
    return model_answer(inputs, {})


async def ready_allocation(client, source, target="22.00"):
    allocation_path, request = await approve_target(client, source, target)
    allocation = success(await client.post(allocation_path, headers=ALICE, json=request))
    assert allocation["status"] == "ready_for_workbook", allocation
    task = allocation_path.removesuffix("/allocation")
    return task + "/workbook", {**START, "expected_allocation_id": allocation["allocation_id"]}


@pytest.mark.anyio
async def test_concurrent_generation_publishes_one_version_and_preserves_source(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", protected_first_model)
    await setup_checkpoints(settings)
    source = allocation_workbook(workbook_with, [
        ("Make Good", "10.00", "Area A"), ("Miscellaneous work", "10.00", "Area A"),
    ])
    async with service() as client:
        path, request = await ready_allocation(client, source)
        assert (await client.post(path, headers=BOB, json=request)).status_code == 404
        responses = await asyncio.gather(*[client.post(path, headers=ALICE, json=request) for _ in range(2)])
        artifact, repeat = [success(item) for item in responses]
        assert repeat == artifact
        assert artifact["status"] == "generated_awaiting_qa"
        assert artifact["source_sha256"] == sha256(source).hexdigest()
        assert artifact["report"]["original_source_integrity"] == "sha256_matched_before_generation"
        assert artifact["report"]["copied_source_fidelity"] == "structural_and_cell_evidence_matched"
        assert artifact["filename"].endswith("Invoice Breakdown r2 v1.xlsx")
        old, = (task_runtime / "outputs").rglob("*.xlsx")
        assert (await client.get(path + "/download", headers=BOB)).status_code == 404

    async with service() as client:
        assert success(await client.get(path, headers=ALICE)) == artifact
        download = await client.get(path + "/download", headers=ALICE)
        assert download.status_code == 200
        assert download.headers["x-workbook-status"] == "generated_awaiting_qa"
        assert download.headers["cache-control"] == "private, no-store"
        assert len(download.content) == artifact["size"]
        assert sha256(download.content).hexdigest() == artifact["sha256"]
        book = load_workbook(BytesIO(download.content), data_only=True)
        assert book.sheetnames[:2] == ["Invoice Breakdown", "Adjustment Detail"]
        task = path.removesuffix("/workbook")
        unchanged = await client.get(task + "/source", headers=ALICE)
        assert unchanged.content == source

        monkeypatch.setattr(workbooks, "TEMPLATE_VERSION", "2")
        assert (await client.get(path, headers=ALICE)).status_code == 404
        new = success(await client.post(path, headers=ALICE, json=request))
        assert new["template_version"] == "2" and new["version"] == 2
        assert new["artifact_id"] != artifact["artifact_id"]
        assert old.read_bytes() == download.content
        monkeypatch.setattr(workbooks, "GENERATOR_VERSION", "2")
        regenerated = success(await client.post(path, headers=ALICE, json=request))
        assert regenerated["generator_version"] == "2" and regenerated["version"] == 3
        assert len(list((task_runtime / "outputs").rglob("*.xlsx"))) == 3


@pytest.mark.anyio
async def test_unsupported_source_feature_is_reported_over_api(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    from test_allocation_api import fixture_model

    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    async with service() as client:
        path, request = await ready_allocation(client, unsupported_drawing(workbook_with), target="260.00")
        response = await client.post(path, headers=ALICE, json=request)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["code"] == "unsupported_source_feature"
        assert (detail["sheet"], detail["feature"]) == ("Notes", "drawing")
        assert (await client.get(path + "/download", headers=ALICE)).status_code == 404
    assert not list((task_runtime / "outputs").rglob("*.xlsx"))


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["partial_blob", "corrupt_blob", "manifest_failure"])
async def test_failed_publication_cleans_unreferenced_output_and_can_retry(
    task_runtime, configured_skill, monkeypatch, workbook_with, mode,
):
    monkeypatch.setattr(classification_model, "propose", misc_model)
    await setup_checkpoints(settings)
    source = allocation_workbook(workbook_with, [("Miscellaneous work", "20.00", "Area A")])
    async with service() as client:
        path, request = await ready_allocation(client, source)
        storage = main.app.state.output_storage
        original_put = storage.put

        def corrupt(key, data):
            original_put(key, data[:20])
            if mode == "partial_blob":
                raise StorageError("simulated transport failure after writing")

        def manifest_failure(*_args, **_kwargs):
            raise SQLAlchemyError("simulated manifest write failure")

        with monkeypatch.context() as patch:
            if mode in {"partial_blob", "corrupt_blob"}:
                patch.setattr(storage, "put", corrupt)
            else:
                patch.setattr(workbooks, "insert", manifest_failure)
            assert (await client.post(path, headers=ALICE, json=request)).status_code == 503
        assert (await client.get(path + "/download", headers=ALICE)).status_code == 404
        assert not list((task_runtime / "outputs").rglob("*.xlsx"))
        stored = success(await client.post(path, headers=ALICE, json=request))
        assert stored["version"] == 1
        assert len(list((task_runtime / "outputs").rglob("*.xlsx"))) == 1


@pytest.mark.anyio
async def test_storage_collision_leaves_preexisting_object_intact(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", misc_model)
    await setup_checkpoints(settings)
    source = allocation_workbook(workbook_with, [("Miscellaneous work", "20.00", "Area A")])
    async with service() as client:
        path, request = await ready_allocation(client, source)
        task_id = path.split("/")[2]
        existing = uuid4()
        key = f"outputs/{task_id}/{existing}.xlsx"
        main.app.state.output_storage.put(key, b"preexisting bytes")
        with monkeypatch.context() as patch:
            patch.setattr(workbooks, "uuid4", lambda: existing)
            assert (await client.post(path, headers=ALICE, json=request)).status_code == 409
        assert (task_runtime / key).read_bytes() == b"preexisting bytes"
        assert (await client.get(path + "/download", headers=ALICE)).status_code == 404
        assert success(await client.post(path, headers=ALICE, json=request))["version"] == 1


@pytest.mark.anyio
async def test_generation_rechecks_allocation_before_writing(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", misc_model)
    await setup_checkpoints(settings)
    source = allocation_workbook(workbook_with, [("Miscellaneous work", "20.00", "Area A")])
    complete, release = asyncio.Event(), asyncio.Event()
    render = workbooks.to_process.run_sync

    async def delayed(*args, **kwargs):
        result = await render(*args, **kwargs)
        complete.set()
        await release.wait()
        return result

    async with service() as client:
        path, request = await ready_allocation(client, source)
        allocation_path = path.removesuffix("/workbook") + "/allocation"
        current = success(await client.get(allocation_path, headers=ALICE))
        monkeypatch.setattr(workbooks.to_process, "run_sync", delayed)
        pending = asyncio.create_task(client.post(path, headers=ALICE, json=request))
        try:
            await asyncio.wait_for(complete.wait(), 8)
            newer = success(await client.post(allocation_path, headers=ALICE, json={
                **START, "expected_classification_id": current["classification_id"],
                "expected_allocation_id": current["allocation_id"],
                "policy": {"requested_direction": "increase"},
            }))
            assert newer["allocation_id"] != current["allocation_id"]
        finally:
            release.set()
            response = await pending
        assert response.status_code == 409
        assert (await client.get(path, headers=ALICE)).status_code == 404
        assert not list((task_runtime / "outputs").rglob("*.xlsx"))
