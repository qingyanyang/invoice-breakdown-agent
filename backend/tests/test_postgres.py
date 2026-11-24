import asyncio
import os
import re
from subprocess import run
from time import monotonic

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text

from app import database, main
from app.database import ALEMBIC_INI, repository_heads


@pytest.mark.anyio
async def test_migrate_empty_schema_and_keep_record_after_restart(disposable_postgres):
    admin, schema = disposable_postgres
    assert not inspect(admin).has_schema(schema)
    config = Config(str(ALEMBIC_INI))

    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        first_engine = main.app.state.database_engine
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503
        assert not inspect(admin).has_schema(schema)

        command.upgrade(config, "head")
        assert (await client.get("/ready")).json() == {"status": "ready"}
        async with first_engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            await connection.exec_driver_sql("CREATE TABLE readiness_probe (value text NOT NULL)")
            await connection.execute(
                text("INSERT INTO readiness_probe (value) VALUES (:value)"),
                {"value": "same disposable database"},
            )

    command.upgrade(config, "head")
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        restarted_engine = main.app.state.database_engine
        assert restarted_engine is not first_engine
        assert (await client.get("/ready")).status_code == 200
        async with restarted_engine.connect() as connection:
            assert await connection.scalar(text("SELECT value FROM readiness_probe")) == (
                "same disposable database"
            )
            assert set(await connection.scalars(text("SELECT version_num FROM alembic_version"))) == (
                repository_heads()
            )

        with admin.begin() as blocker:
            # Keep a broken readiness timeout from holding up the test indefinitely.
            blocker.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '7s'")
            blocker.exec_driver_sql(
                f'LOCK TABLE "{schema}".alembic_version IN ACCESS EXCLUSIVE MODE'
            )
            started = monotonic()
            response = await client.get("/ready")
            assert monotonic() - started < 6
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
        assert (await client.get("/ready")).status_code == 200


@pytest.mark.anyio
async def test_readiness_closes_stalled_checkout(disposable_postgres, monkeypatch, caplog):
    container_id = os.environ.get("TEST_POSTGRES_CONTAINER_ID")
    if not container_id:
        pytest.skip("TEST_POSTGRES_CONTAINER_ID is required for the stalled-backend test")
    if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
        pytest.fail("Expected a disposable PostgreSQL container ID", pytrace=False)

    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    monkeypatch.setattr(database, "READINESS_TIMEOUT_SECONDS", 0.5)
    async with (
        main.lifespan(main.app),
        AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client,
    ):
        engine = main.app.state.database_engine
        async with engine.connect() as connection:
            backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        assert engine.pool.checkedin() == 1

        def signal_backend(signal):
            run(
                [
                    "docker", "exec", "--user", "postgres", container_id,
                    "sh", "-c", 'kill "$1" "$2"', "--", signal, str(backend_pid),
                ],
                check=True,
                timeout=5,
            )

        resumed = False

        def resume_backend():
            nonlocal resumed
            if not resumed:
                signal_backend("-CONT")
                resumed = True

        signal_backend("-STOP")
        # Let a broken probe finish so the failure reports its actual runtime.
        watchdog = asyncio.get_running_loop().call_later(3, resume_backend)
        try:
            started = monotonic()
            response = await client.get("/ready")
            assert monotonic() - started < 2
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
            assert "Database readiness check timed out" in caplog.text
            assert engine.pool.checkedout() == 0
        finally:
            watchdog.cancel()
            resume_backend()

        assert (await client.get("/ready")).status_code == 200
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT pg_backend_pid()")) != backend_pid
