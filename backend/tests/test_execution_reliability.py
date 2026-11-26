import asyncio

import pytest

from test_classification_api import START, configured_skill
from test_task_sources import ALICE, BOB, running_service


@pytest.mark.anyio
async def test_create_key_replays_once_across_restart_and_isolates_operators(task_runtime):
    key = {"Idempotency-Key": "create-receipt-1"}
    first_body = {"target": {"amount": "25.10", "gst_basis": "unknown"}}
    equivalent_body = {"operation": "create", "target": {"amount": "25.1", "gst_basis": "unknown"}}
    async with running_service() as client:
        replies = await asyncio.gather(
            client.post("/tasks", headers=ALICE | key, json=first_body),
            client.post("/tasks", headers=ALICE | key, json=equivalent_body),
        )
        assert [reply.status_code for reply in replies] == [201, 201]
        task_id = replies[0].json()["id"]
        assert replies[1].json()["id"] == task_id

    async with running_service() as client:
        replay = await client.post("/tasks", headers=ALICE | key, json=equivalent_body)
        assert replay.status_code == 201 and replay.json()["id"] == task_id
        conflict = await client.post("/tasks", headers=ALICE | key, json={})
        assert conflict.status_code == 409
        other = await client.post("/tasks", headers=BOB | key, json=first_body)
        assert other.status_code == 201 and other.json()["id"] != task_id
        assert (await client.get(f"/tasks/{task_id}", headers=BOB)).status_code == 404


@pytest.mark.anyio
async def test_expired_workbook_worker_cannot_publish_or_delete_replacement(
    task_runtime, disposable_postgres, configured_skill, monkeypatch,
):
    from hashlib import sha256
    from threading import Event

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from app import main, source_cleanup
    from app.features.classification import model as classification_model
    from app.features.workbooks.routes import router as workbook_router
    from app.checkpoints import setup_checkpoints
    from app.config import settings
    from test_allocation_api import approve_target, fixture_model
    from test_reconciliation_api import FIXTURES, success

    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    paused, release = Event(), Event()
    replacement_app = FastAPI()
    replacement_app.include_router(workbook_router)
    try:
        async with running_service() as first_client:
            source = (FIXTURES / "reconciliation.xlsx").read_bytes()
            allocation_path, approval = await approve_target(first_client, source, "282.54")
            allocation = success(await first_client.post(allocation_path, headers=ALICE, json=approval))
            path = allocation_path.removesuffix("/allocation") + "/workbook"
            payload = {**START, "expected_allocation_id": allocation["allocation_id"]}
            original_put = main.app.state.output_storage.put
            stale_keys = []

            def paused_put(key, data):
                stale_keys.append(key)
                original_put(key, data)
                paused.set()
                assert release.wait(10), "test did not release the stale writer"

            monkeypatch.setattr(main.app.state.output_storage, "put", paused_put)
            stale = asyncio.create_task(first_client.post(path, headers=ALICE, json=payload))
            try:
                assert await asyncio.to_thread(paused.wait, 8)
                assert not await source_cleanup.cleanup(stale_keys[0])
                assert (task_runtime / stale_keys[0]).is_file()
                async with (
                    main.lifespan(replacement_app),
                    AsyncClient(transport=ASGITransport(app=replacement_app), base_url="http://test") as second_client,
                ):
                    monkeypatch.setattr(settings, "execution_duplicate_wait_seconds", 0)
                    in_progress = await second_client.post(path, headers=ALICE, json=payload)
                    assert in_progress.status_code == 202
                    assert in_progress.headers["retry-after"]
                    assert in_progress.json()["detail"]["status"] == "running"

                    database, schema = disposable_postgres
                    with database.begin() as connection:
                        result = connection.execute(text(
                            f'UPDATE "{schema}".execution_runs SET lease_expires_at = clock_timestamp() '
                            "WHERE id = :operation_id AND status = 'running'"
                        ), {"operation_id": in_progress.headers["x-execution-id"]})
                        assert result.rowcount == 1
                    winning = success(await second_client.post(path, headers=ALICE, json=payload))
                    release.set()
                    rejected = await asyncio.wait_for(stale, 10)
                    assert rejected.status_code == 409
                    assert rejected.json()["detail"] == "Execution attempt is no longer current"
                    assert success(await second_client.get(path, headers=ALICE)) == winning
                    downloaded = await second_client.get(path + "/download", headers=ALICE)
                    assert downloaded.status_code == 200
                    assert sha256(downloaded.content).hexdigest() == winning["sha256"]

                execution_path = path.removesuffix("/workbook") + "/executions"
                executions = success(await first_client.get(execution_path, headers=ALICE))
                operation = next(item for item in executions if item["id"] == in_progress.headers["x-execution-id"])
                assert operation["attempt"] == 2 and operation["result"]["artifact_id"] == winning["artifact_id"]
                assert len(stale_keys) == 1
                assert not (task_runtime / stale_keys[0]).exists()
                assert len(list((task_runtime / "outputs").rglob("*.xlsx"))) == 1
            finally:
                release.set()
                await asyncio.gather(stale, return_exceptions=True)
    finally:
        release.set()


@pytest.mark.anyio
async def test_upload_receipt_binds_bytes_and_replays_a_historical_revision(task_runtime):
    from test_task_sources import FIXTURES, create_task, upload

    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    other = (FIXTURES / "scope-ambiguity.xlsx").read_bytes()
    headers = ALICE | {"Idempotency-Key": "upload-receipt-1"}

    async with running_service() as client:
        task = await create_task(client)
        first = await upload(client, task["id"], source, headers=headers)
        assert first.status_code == 200
        edited = await client.put(f"/tasks/{task['id']}/target", headers=ALICE, json={
            "expected_revision": 2, "target": {"amount": "60", "gst_basis": "excluding_gst"},
        })
        assert edited.status_code == 200 and edited.json()["revision"] == 3

    async with running_service() as client:
        replay = await upload(client, task["id"], source, headers=headers)
        assert replay.status_code == 200 and replay.json() == first.json()
        conflict = await upload(client, task["id"], other, headers=headers)
        assert conflict.status_code == 409
        assert "different input" in conflict.text
        assert (await client.get(f"/tasks/{task['id']}", headers=ALICE)).json()["revision"] == 3
        assert (await client.get(f"/tasks/{task['id']}/source", headers=ALICE)).content == source
    assert len(list(task_runtime.rglob("*.xlsx"))) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("limit", ["attempt_limit", "deadline"])
async def test_transient_extraction_attempts_exhaust_across_restarts(
    task_runtime, disposable_postgres, monkeypatch, workbook_with, limit,
):
    from sqlalchemy import text

    from app.checkpoints import setup_checkpoints
    from app.config import settings
    from app.source_storage import LocalSourceStorage, StorageError
    from test_extractions import create_ready_task

    monkeypatch.setattr(settings, "execution_max_attempts", 2)
    monkeypatch.setattr(settings, "execution_retry_base_seconds", 0.01)
    await setup_checkpoints(settings)
    calls = 0

    def unavailable(*_args):
        nonlocal calls
        calls += 1
        raise StorageError("private storage failure")

    monkeypatch.setattr(LocalSourceStorage, "read", unavailable)
    async with running_service() as client:
        task_id = await create_ready_task(client, workbook_with({}))
        path = f"/tasks/{task_id}"
        first = await client.post(path + "/extraction", json=START, headers=ALICE)
        assert first.status_code == 503 and "private storage failure" not in first.text

    if limit == "deadline":
        database, schema = disposable_postgres
        with database.begin() as connection:
            count = connection.execute(text(
                f'UPDATE "{schema}".execution_runs SET deadline = clock_timestamp() '
                "WHERE id = :operation_id"
            ), {"operation_id": first.headers["x-execution-id"]})
            assert count.rowcount == 1

    async with running_service() as client:
        expected_calls = 1
        if limit == "attempt_limit":
            second = await client.post(path + "/extraction", json=START, headers=ALICE)
            assert second.status_code == 503
            assert second.headers["x-execution-id"] == first.headers["x-execution-id"]
            expected_calls = 2
        stopped = await client.post(path + "/extraction", json=START, headers=ALICE)
        assert stopped.status_code == 503
        assert stopped.json()["detail"]["status"] == "exhausted"
        assert stopped.json()["detail"]["exhaustion_reason"] == limit
        assert calls == expected_calls
        outcomes = (await client.get(path + "/executions", headers=ALICE)).json()
        extraction = next(row for row in outcomes if row["operation"] == "extraction")
        assert (extraction["status"], extraction["attempt"], extraction["failure_code"]) == (
            "exhausted", expected_calls, "source_unavailable",
        )
        assert "worker_token" not in extraction and "operator" not in extraction
        assert (await client.get(path + "/executions", headers=BOB)).status_code == 404


@pytest.mark.anyio
async def test_accepted_answer_has_its_own_budget_and_replays_after_revision_change(
    task_runtime, disposable_postgres, workbook_with,
):
    from sqlalchemy import text

    from app.checkpoints import setup_checkpoints
    from app.config import settings
    from test_workflows import pause_for_gst

    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id, question = await pause_for_gst(client, workbook_with({}))
        path = f"/tasks/{task_id}"
        database, schema = disposable_postgres
        with database.begin() as connection:
            result = connection.execute(text(
                f'UPDATE "{schema}".execution_runs SET deadline = clock_timestamp() '
                "WHERE operation = 'checkpoint_start' AND status = 'blocked'"
            ))
            assert result.rowcount == 1

        key = {"Idempotency-Key": "gst-answer-1"}
        answer = {**START, "question_id": question["id"], "answer": "ex_gst"}
        response = await client.post(path + "/workflow/answers", headers=ALICE | key, json=answer)
        assert response.status_code == 200 and response.json()["status"] == "ready_for_extraction"
        outcomes = (await client.get(path + "/executions", headers=ALICE)).json()
        phases = {row["operation"]: (row["status"], row["attempt"]) for row in outcomes}
        assert phases == {"checkpoint_start": ("blocked", 1), "checkpoint_resume": ("succeeded", 1)}

        changed = await client.put(path + "/target", headers=ALICE, json={
            "expected_revision": 2, "target": {"amount": "250", "gst_basis": "unknown"},
        })
        assert changed.status_code == 200 and changed.json()["revision"] == 3
        replay = await client.post(path + "/workflow/answers", headers=ALICE | key, json=answer)
        assert replay.status_code == 200 and replay.json() == response.json()
        conflict = await client.post(path + "/workflow/answers", headers=ALICE | key, json={**answer, "answer": "inc_gst"})
        assert conflict.status_code == 409
        workflow = await client.get(path + "/workflow", headers=ALICE)
        assert workflow.json()["status"] == "not_started"


@pytest.mark.anyio
async def test_upload_deadline_releases_request_and_fences_late_storage(task_runtime, monkeypatch):
    from threading import Event
    from uuid import UUID

    from app import source_cleanup
    from app.config import settings
    from app.source_storage import LocalSourceStorage
    from test_task_sources import FIXTURES, create_task, upload

    monkeypatch.setattr(settings, "execution_upload_seconds", 1)
    written, release, finished = Event(), Event(), Event()
    original = LocalSourceStorage.put

    def slow_put(storage, key, data):
        original(storage, key, data)
        written.set()
        try:
            assert release.wait(8), "test did not release the storage worker"
        finally:
            finished.set()

    monkeypatch.setattr(LocalSourceStorage, "put", slow_put)
    async with running_service() as client:
        task = await create_task(client)
        task_path = f"/tasks/{task['id']}"
        pending = asyncio.create_task(upload(client, task["id"], (FIXTURES / "reconciliation.xlsx").read_bytes()))
        try:
            assert await asyncio.to_thread(written.wait, 3)
            failed = await asyncio.wait_for(pending, 4)
            assert failed.status_code == 504 and not finished.is_set()
            assert (await client.get(task_path, headers=ALICE)).json()["revision"] == 1
            outcomes = (await client.get(task_path + "/executions", headers=ALICE)).json()
            assert outcomes[0]["failure_code"] == "attempt_deadline"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 4)
        assert await source_cleanup.cleanup_execution(UUID(failed.headers["x-execution-id"])) == (1, 0)
        assert not list(task_runtime.rglob("*.xlsx"))
        assert (await client.get(task_path + "/source", headers=ALICE)).status_code == 404
