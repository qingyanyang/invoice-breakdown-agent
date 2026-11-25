import asyncio
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app import classification_model, reconciliations, source_cleanup, workbooks
from app import tasks as task_api
from app.checkpoints import setup_checkpoints
from app.config import settings
from app.execution import Unavailable
from app.source_storage import LocalSourceStorage, StorageError
from app.workbook_source import WorkbookProblem
from test_allocation_api import fixture_model
from test_classification_api import START, configured_skill
from test_reconciliation_api import create_source, establish_evidence, success
from test_task_sources import ALICE, BOB, FIXTURES, create_task, running_service, upload
from test_workbooks_api import ready_allocation


@pytest.mark.anyio
async def test_expired_worker_cannot_end_execution_before_takeover(
    task_runtime, disposable_postgres, configured_skill, monkeypatch,
):
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    generating, release = asyncio.Event(), asyncio.Event()
    generate = workbooks.to_process.run_sync

    async def late_failure(function, *args, **kwargs):
        if function is workbooks.generate_workbook and not generating.is_set():
            generating.set()
            await release.wait()
            raise WorkbookProblem("late_worker_failure", "Synthetic terminal failure")
        return await generate(function, *args, **kwargs)

    monkeypatch.setattr(workbooks.to_process, "run_sync", late_failure)
    async with running_service() as client:
        path, payload = await ready_allocation(client, (FIXTURES / "reconciliation.xlsx").read_bytes(), "282.54")
        execution_path = path.removesuffix("/workbook") + "/executions"
        old = asyncio.create_task(client.post(path, headers=ALICE, json=payload))
        try:
            await asyncio.wait_for(generating.wait(), 5)
            runs = success(await client.get(execution_path, headers=ALICE))
            run_id = next(row["id"] for row in runs if row["operation"] == "workbook")
            database, schema = disposable_postgres
            with database.begin() as connection:
                connection.execute(text(
                    f'UPDATE "{schema}".execution_runs SET lease_expires_at = clock_timestamp() WHERE id = :id',
                ), {"id": run_id})
            release.set()
            late = await asyncio.wait_for(old, 5)
            assert late.status_code in {409, 422}
            replacement = await client.post(path, headers=ALICE, json=payload)
            assert replacement.status_code == 200, replacement.text
            record = next(row for row in success(await client.get(execution_path, headers=ALICE)) if row["id"] == run_id)
            assert record["attempt"] == 2 and record["status"] == "succeeded"
            assert record["result"]["artifact_id"] == replacement.json()["artifact_id"]
        finally:
            release.set()
            await asyncio.gather(old, return_exceptions=True)


@pytest.mark.anyio
async def test_waiting_reconciliation_duplicate_cannot_rebase_its_scope_change(
    task_runtime, monkeypatch,
):
    await setup_checkpoints(settings)
    a_started, b_started, b_waiting, release_a, release_b = [asyncio.Event() for _ in range(5)]
    key_a = ALICE | {"Idempotency-Key": "reconcile-a"}
    key_b = ALICE | {"Idempotency-Key": "reconcile-b"}
    scope_a = {"exclude": ["Trade Summary!D4"]}
    scope_b = {"exclude": ["Trade Summary!D5"]}
    run_process, reserve_claim = reconciliations.to_process.run_sync, reconciliations.claim
    b_calls = 0

    async def scheduled(function, *args, **kwargs):
        nonlocal b_calls
        if function is reconciliations.reconcile:
            if args[1]["exclude"] == scope_a["exclude"]:
                a_started.set()
                await release_a.wait()
            if args[1]["exclude"] == scope_b["exclude"]:
                b_calls += 1
                if b_calls == 1:
                    b_started.set()
                    await release_b.wait()
        return await run_process(function, *args, **kwargs)

    async def observe_claim(*args, **kwargs):
        claim = await reserve_claim(*args, **kwargs)
        if isinstance(claim, Unavailable) and claim.status == "running":
            b_waiting.set()
        return claim

    monkeypatch.setattr(reconciliations.to_process, "run_sync", scheduled)
    monkeypatch.setattr(reconciliations, "claim", observe_claim)
    async with running_service() as client:
        task_id = await create_source(client, "reconciliation.xlsx")
        await establish_evidence(client, task_id)
        path = f"/tasks/{task_id}/reconciliation"
        success(await client.post(path, headers=ALICE, json=START))
        a = asyncio.create_task(client.post(path, headers=key_a, json={**START, "scope": scope_a}))
        b = asyncio.create_task(client.post(path, headers=key_b, json={**START, "scope": scope_b}))
        jobs = [a, b]
        try:
            await asyncio.wait_for(a_started.wait(), 5)
            await asyncio.wait_for(b_started.wait(), 5)
            duplicate = asyncio.create_task(client.post(path, headers=key_b, json={**START, "scope": scope_b}))
            jobs.append(duplicate)
            await asyncio.wait_for(b_waiting.wait(), 5)
            release_a.set()
            winning = success(await asyncio.wait_for(a, 5))
            rejected = await asyncio.wait_for(duplicate, 5)
            assert rejected.status_code == 409, rejected.text
            release_b.set()
            assert (await asyncio.wait_for(b, 5)).status_code == 409
            assert b_calls == 1
            assert success(await client.get(path, headers=ALICE)) == winning
        finally:
            release_a.set()
            release_b.set()
            await asyncio.gather(*jobs, return_exceptions=True)

    async with running_service() as client:
        assert (await client.post(path, headers=key_b, json={**START, "scope": scope_b})).status_code == 409
        assert success(await client.post(path, headers=key_a, json={**START, "scope": scope_a})) == winning


@pytest.mark.anyio
async def test_upload_validation_deadline_prevents_claim_and_retains_validator_slot(task_runtime, monkeypatch):
    monkeypatch.setattr(settings, "execution_upload_seconds", 0.25)
    original = task_api.validate_workbook
    started, release = Event(), Event()
    calls = 0

    def slow_validation(data):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(5), "test did not release the validator"
        original(data)

    monkeypatch.setattr(task_api, "validate_workbook", slow_validation)
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        path = f"/tasks/{task_id}"
        first = asyncio.create_task(upload(client, task_id, source))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            expired = await asyncio.wait_for(asyncio.shield(first), 2)
            assert expired.status_code == 504
            assert (await upload(client, task_id, source)).status_code == 504
            assert calls == 1
            assert success(await client.get(path + "/executions", headers=ALICE)) == []
            assert success(await client.get(path, headers=ALICE))["revision"] == 1
            assert not list(task_runtime.rglob("*.xlsx"))
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("expected_state", ["collision", "unconfirmed"])
async def test_timed_out_storage_failure_does_not_grant_cleanup_ownership(
    task_runtime, disposable_postgres, monkeypatch, expected_state,
):
    monkeypatch.setattr(settings, "execution_upload_seconds", 1)
    original = LocalSourceStorage.put
    started, release, finished = Event(), Event(), Event()
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    artifact_id = uuid4()

    def late_put(storage, key, data):
        started.set()
        assert release.wait(5), "test did not release the writer"
        try:
            if expected_state == "unconfirmed":
                raise StorageError("Synthetic failure without a write acknowledgment")
            original(storage, key, data)
        finally:
            finished.set()

    monkeypatch.setattr(LocalSourceStorage, "put", late_put)
    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        key = f"sources/{task_id}/{artifact_id}.xlsx"
        existing = task_runtime / key
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"preexisting content")
        monkeypatch.setattr(task_api, "uuid4", lambda: artifact_id)
        pending = asyncio.create_task(upload(client, task_id, source))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            timed_out = await asyncio.wait_for(pending, 4)
            assert timed_out.status_code == 504 and not finished.is_set()
            execution_id = UUID(timed_out.headers["x-execution-id"])
            assert await source_cleanup.cleanup_execution(execution_id) == (0, 1)
            assert existing.read_bytes() == b"preexisting content"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    assert finished.is_set()
    database, schema = disposable_postgres
    with database.connect() as connection:
        state = connection.scalar(text(
            f'SELECT write_state FROM "{schema}".execution_objects WHERE storage_key = :key',
        ), {"key": key})
    assert state == expected_state
    assert await source_cleanup.cleanup_execution(execution_id) == (0, 1)
    assert existing.read_bytes() == b"preexisting content"


@pytest.mark.anyio
async def test_history_reports_expired_execution_cannot_resume_after_revision_change(
    task_runtime, disposable_postgres, monkeypatch,
):
    original = LocalSourceStorage.put
    started, release = Event(), Event()
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()

    def paused_put(storage, key, data):
        started.set()
        assert release.wait(5), "test did not release the writer"
        original(storage, key, data)

    monkeypatch.setattr(LocalSourceStorage, "put", paused_put)
    async with running_service() as client:
        task_id = (await create_task(client))["id"]
        path = f"/tasks/{task_id}"
        pending = asyncio.create_task(upload(client, task_id, source))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            run, = success(await client.get(path + "/executions?revision=1", headers=ALICE))
            changed = await client.put(path + "/target", headers=ALICE, json={
                "expected_revision": 1, "target": {"amount": "120", "gst_basis": "excluding_gst"},
            })
            assert success(changed)["revision"] == 2
            database, schema = disposable_postgres
            with database.begin() as connection:
                connection.execute(text(
                    f'UPDATE "{schema}".execution_runs '
                    "SET lease_expires_at = clock_timestamp(), deadline = clock_timestamp() WHERE id = :id",
                ), {"id": run["id"]})
            history, = success(await client.get(path + "/executions?revision=1", headers=ALICE))
            assert history["status"] == "exhausted"
            assert history["exhaustion_reason"] == "deadline"
            assert (await client.get(path + "/executions?revision=1", headers=BOB)).status_code == 404
            assert success(await client.get(path + "/executions", headers=ALICE)) == []
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
