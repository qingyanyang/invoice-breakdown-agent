import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import OperationalError
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app import main, workflows
from app.checkpoints import open_checkpointer, setup_checkpoints
from app.config import settings
from app.database import ALEMBIC_INI
from app.invoice_graph import compile_invoice_graph

ALICE = {"Authorization": "Bearer " + "a" * 40}
BOB = {"Authorization": "Bearer " + "b" * 40}


@asynccontextmanager
async def running_service():
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        yield client


def assert_success(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


async def create_task(client, basis="unknown"):
    target = {"amount": "125.10", "gst_basis": basis} if basis else None
    response = await client.post("/tasks", json={"target": target}, headers=ALICE)
    return assert_success(response, 201)["id"]


async def upload(client, task_id, workbook, *, revision=1):
    return assert_success(await client.put(
        f"/tasks/{task_id}/source",
        params={"expected_revision": revision, "filename": "sample.xlsx"},
        content=workbook, headers=ALICE,
    ))


async def start(client, task_id, revision=2, headers=ALICE):
    return await client.post(
        f"/tasks/{task_id}/workflow", json={"expected_revision": revision}, headers=headers,
    )


async def answer(client, task_id, question, value="ex_gst", *, revision=2, headers=ALICE):
    return await client.post(
        f"/tasks/{task_id}/workflow/answers",
        json={"expected_revision": revision, "question_id": question, "answer": value},
        headers=headers,
    )


async def pause_for_gst(client, workbook):
    task_id = await create_task(client)
    await upload(client, task_id, workbook)
    details = assert_success(await start(client, task_id))
    assert details["status"] == "awaiting_clarification"
    return task_id, details["question"]


@pytest.mark.anyio
async def test_checkpoints_require_explicit_setup(task_runtime, disposable_postgres, workbook_with):
    admin, schema = disposable_postgres
    async with running_service() as client:
        task_id = await create_task(client)
        assert (await start(client, task_id, revision=1)).status_code == 409
        await upload(client, task_id, workbook_with({}))
        response = await start(client, task_id)
        assert response.status_code == 503
        assert response.json() == {"detail": "Workflow checkpoint storage unavailable"}
        current = assert_success(await client.get(f"/tasks/{task_id}/workflow", headers=ALICE))
        assert current["status"] == "not_started"
    assert not inspect(admin).has_table("checkpoints", schema=schema)
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0002_task_sources")
    command.upgrade(config, "head")
    await setup_checkpoints(settings)
    await setup_checkpoints(settings)
    assert inspect(admin).has_table("checkpoints", schema=schema)


@pytest.mark.anyio
async def test_pause_restart_and_concurrent_duplicate_answers(
    task_runtime, disposable_postgres, workbook_with,
):
    admin, schema = disposable_postgres
    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id, question = await pause_for_gst(client, workbook_with({}))
        assert question["reason"] == "target_gst_basis_unknown"
        assert question["evidence"] == {
            "kind": "submitted_target", "amount": "125.10", "gst_basis": "unknown",
        }
        assert question["options"] == ["ex_gst", "inc_gst"]
        assert assert_success(await start(client, task_id))["question"]["id"] == question["id"]

    async with running_service() as client:
        assert (await start(client, task_id, headers=BOB)).status_code == 404
        assert (await answer(client, task_id, question["id"], headers=BOB)).status_code == 404
        assert (await answer(client, task_id, str(uuid4()))).status_code == 404
        assert (await answer(client, task_id, question["id"], "natural language")).status_code == 422
        failed_status = assert_success(await client.get(f"/tasks/{task_id}/workflow", headers=ALICE))
        assert failed_status["status"] == "awaiting_clarification"
        first, second = await asyncio.gather(
            answer(client, task_id, question["id"]), answer(client, task_id, question["id"]),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        ready = (first if first.status_code == 200 else second).json()
        assert ready["status"] == "ready_for_extraction"
        assert ready["revision"] == 2
        assert ready["effective_gst_basis"] == "excluding_gst"
        assert ready["question"]["answer"] == "ex_gst"
        task = assert_success(await client.get(f"/tasks/{task_id}", headers=ALICE))
        assert task["revision"] == 2 and task["target"]["gst_basis"] == "unknown"

    with admin.connect() as connection:
        assert connection.scalar(text(f'SELECT count(*) FROM "{schema}".workflow_answers')) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["source", "target"])
async def test_changed_inputs_invalidate_previous_question(task_runtime, workbook_with, mutation):
    workbook = workbook_with({})
    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id, old_question = await pause_for_gst(client, workbook)
        if mutation == "source":
            revision = await upload(client, task_id, workbook, revision=2)
        else:
            revision = assert_success(await client.put(
                f"/tasks/{task_id}/target",
                json={"expected_revision": 2, "target": {"amount": "500.00", "gst_basis": "unknown"}},
                headers=ALICE,
            ))
        assert revision["revision"] == 3
        assert (await answer(client, task_id, old_question["id"])).status_code == 409
        assert assert_success(await start(client, task_id, revision=3))["question"]["id"] != old_question["id"]
        assert (await answer(client, task_id, old_question["id"], revision=3)).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("basis", [None, "including_gst"])
async def test_known_or_absent_target_skips_clarification(task_runtime, workbook_with, basis):
    await setup_checkpoints(settings)
    async with running_service() as client:
        task_id = await create_task(client, basis)
        await upload(client, task_id, workbook_with({}))
        ready = assert_success(await start(client, task_id))
        assert ready["status"] == "ready_for_extraction"
        assert ready["question"] is None and ready["effective_gst_basis"] == basis


async def checkpoint_id(task_id):
    async with open_checkpointer(settings) as saver:
        snapshot = await saver.aget_tuple({"configurable": {"thread_id": f"invoice:{task_id}:2"}})
        return snapshot.config["configurable"]["checkpoint_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("checkpoint_already_advanced", [False, True])
async def test_recovery_uses_accepted_answer_after_restart(
    task_runtime, workbook_with, monkeypatch, checkpoint_already_advanced,
):
    await setup_checkpoints(settings)
    original = workflows.resume_accepted

    async def interrupted(*args):
        if checkpoint_already_advanced:
            await original(*args)
            raise SQLAlchemyError("simulated transaction interruption after checkpoint")
        raise HTTPException(503, "simulated interruption before checkpoint")

    async with running_service() as client:
        task_id, question = await pause_for_gst(client, workbook_with({}))
        with monkeypatch.context() as patch:
            patch.setattr(workflows, "resume_accepted", interrupted)
            assert (await answer(client, task_id, question["id"], "inc_gst")).status_code == 503
        current = assert_success(await client.get(f"/tasks/{task_id}/workflow", headers=ALICE))
        assert current["status"] == "answer_accepted"
        assert current["question"]["answer"] == "inc_gst"
    before = await checkpoint_id(task_id)

    resumed = 0

    async def count_resume(*args):
        nonlocal resumed
        resumed += 1
        return await original(*args)

    monkeypatch.setattr(workflows, "resume_accepted", count_resume)
    async with running_service() as client:
        first, second = await asyncio.gather(start(client, task_id), start(client, task_id))
        for response in (first, second):
            ready = assert_success(response)
            assert ready["status"] == "ready_for_extraction"
            assert ready["revision"] == 2 and ready["effective_gst_basis"] == "including_gst"
        assert (await answer(client, task_id, question["id"], "inc_gst")).status_code == 409
    assert resumed == 1
    assert (await checkpoint_id(task_id) == before) == checkpoint_already_advanced


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["start", "answer"])
async def test_resume_after_checkpoint_snapshot_write_fails(
    task_runtime, workbook_with, monkeypatch, phase,
):
    await setup_checkpoints(settings)
    original_put = AsyncPostgresSaver.aput
    original_put_writes = AsyncPostgresSaver.aput_writes
    persisted_writes = defaultdict(asyncio.Event)

    async def track_writes(saver, config, writes, task_id, task_path=""):
        await original_put_writes(saver, config, writes, task_id, task_path)
        persisted_writes[config["configurable"]["checkpoint_id"]].set()

    async def fail_snapshot(saver, config, checkpoint, metadata, new_versions):
        if phase == "start":
            fail = metadata["source"] == "loop" and metadata["step"] == 0
        else:
            fail = checkpoint["channel_values"].get("stage") == "ready_for_extraction"
        if fail:
            parent_id = config["configurable"]["checkpoint_id"]
            await asyncio.wait_for(persisted_writes[parent_id].wait(), timeout=5)
            raise OperationalError("simulated failure after saving node writes")
        return await original_put(saver, config, checkpoint, metadata, new_versions)

    async with running_service() as client:
        if phase == "start":
            task_id = await create_task(client)
            await upload(client, task_id, workbook_with({}))
        else:
            task_id, question = await pause_for_gst(client, workbook_with({}))
        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput_writes", track_writes)
            patch.setattr(AsyncPostgresSaver, "aput", fail_snapshot)
            if phase == "start":
                response = await start(client, task_id)
            else:
                response = await answer(client, task_id, question["id"], "inc_gst")
            assert response.status_code == 503, response.text

    config = {"configurable": {"thread_id": f"invoice:{task_id}:2"}}
    async with open_checkpointer(settings) as saver:
        saved = await saver.aget_tuple(config)
        snapshot = await compile_invoice_graph(saver).aget_state(config)
        assert saved.pending_writes
        assert not snapshot.next and snapshot.tasks

    async with running_service() as client:
        before = assert_success(await client.get(f"/tasks/{task_id}/workflow", headers=ALICE))
        recovered = assert_success(await start(client, task_id))
        assert recovered["revision"] == 2
        if phase == "start":
            assert before["status"] == "not_started"
            assert recovered["status"] == "awaiting_clarification"
            assert recovered["question"]["answer"] is None
        else:
            assert before["status"] == "answer_accepted"
            assert recovered["status"] == "ready_for_extraction"
            assert recovered["effective_gst_basis"] == "including_gst"
            assert recovered["question"]["id"] == question["id"]


@pytest.mark.anyio
async def test_downgrade_then_upgrade_restarts_retained_task(
    task_runtime, workbook_with,
):
    await setup_checkpoints(settings)
    workbook = workbook_with({})
    async with running_service() as client:
        task_id, old_question = await pause_for_gst(client, workbook)
        ready = assert_success(await answer(client, task_id, old_question["id"]))
        assert ready["status"] == "ready_for_extraction"

    unrelated_config = {"configurable": {"thread_id": "unrelated-workflow"}}
    async with open_checkpointer(settings) as saver:
        graph = compile_invoice_graph(saver)
        await graph.ainvoke(
            {"source_sha256": "fixture", "target_amount": None, "submitted_gst_basis": None},
            config=unrelated_config,
        )

    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0002_task_sources")
    command.upgrade(config, "head")
    await setup_checkpoints(settings)

    async with open_checkpointer(settings) as saver:
        assert await saver.aget_tuple(unrelated_config)
        assert await saver.aget_tuple({"configurable": {"thread_id": f"invoice:{task_id}:2"}}) is None

    async with running_service() as client:
        source = await client.get(f"/tasks/{task_id}/source", headers=ALICE)
        assert source.status_code == 200 and source.content == workbook
        before = assert_success(await client.get(f"/tasks/{task_id}/workflow", headers=ALICE))
        assert before["revision"] == 2 and before["status"] == "not_started"
        pending = assert_success(await start(client, task_id))
        assert pending["status"] == "awaiting_clarification"
        assert pending["question"]["id"] != old_question["id"]
        assert (await answer(client, task_id, old_question["id"])).status_code == 404
        ready = assert_success(await answer(client, task_id, pending["question"]["id"], "inc_gst"))
        assert ready["status"] == "ready_for_extraction" and ready["effective_gst_basis"] == "including_gst"
