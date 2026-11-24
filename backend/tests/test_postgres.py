import os
from time import monotonic
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import DropSchema

from app import main
from app.config import settings
from app.database import ALEMBIC_INI, repository_heads


@pytest.fixture
def disposable_postgres(monkeypatch):
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for disposable PostgreSQL tests")

    # Refuse remote databases and connection options that can redirect the connection.
    try:
        url = make_url(raw_url)
    except (ArgumentError, ValueError):
        pytest.fail("Invalid TEST_DATABASE_URL", pytrace=False)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.database != "invoice_breakdown_test"
        or url.query
    ):
        pytest.fail("Use a disposable invoice_breakdown_test database on 127.0.0.1", pytrace=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)

    schema = f"test_invoice_{uuid4().hex}"
    monkeypatch.setattr(settings, "database_url", raw_url)
    monkeypatch.setattr(settings, "postgres_schema", schema)
    admin = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        yield admin, schema
    finally:
        try:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            admin.dispose()


def test_migrate_empty_schema_and_keep_record_after_restart(disposable_postgres):
    admin, schema = disposable_postgres
    assert not inspect(admin).has_schema(schema)
    config = Config(str(ALEMBIC_INI))

    with TestClient(main.app) as client:
        first_engine = main.app.state.database_engine
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert not inspect(admin).has_schema(schema)

        command.upgrade(config, "head")
        assert client.get("/ready").json() == {"status": "ready"}
        with first_engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            connection.exec_driver_sql("CREATE TABLE readiness_probe (value text NOT NULL)")
            connection.execute(
                text("INSERT INTO readiness_probe (value) VALUES (:value)"),
                {"value": "same disposable database"},
            )

    command.upgrade(config, "head")
    with TestClient(main.app) as client:
        restarted_engine = main.app.state.database_engine
        assert restarted_engine is not first_engine
        assert client.get("/ready").status_code == 200
        with restarted_engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM readiness_probe")) == (
                "same disposable database"
            )
            assert set(connection.scalars(text("SELECT version_num FROM alembic_version"))) == (
                repository_heads()
            )

        with admin.begin() as blocker:
            # Keep a broken readiness timeout from holding up the test indefinitely.
            blocker.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '7s'")
            blocker.exec_driver_sql(
                f'LOCK TABLE "{schema}".alembic_version IN ACCESS EXCLUSIVE MODE'
            )
            started = monotonic()
            response = client.get("/ready")
            assert monotonic() - started < 6
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
        assert client.get("/ready").status_code == 200
