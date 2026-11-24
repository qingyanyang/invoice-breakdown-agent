import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import DropSchema

from app import main, source_cleanup
from app.config import settings
from app.database import ALEMBIC_INI, metadata


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def inherited_service_profile(monkeypatch, tmp_path):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[redirect]\nhostaddr=127.0.0.2\nsslmode=require\n")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "redirect")


@pytest.fixture
def disposable_postgres(monkeypatch, inherited_service_profile):
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
    monkeypatch.delenv("PGSERVICE", raising=False)

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


@pytest.fixture
def workbook_with():
    source = (
        Path(__file__).resolve().parents[2]
        / "tests" / "fixtures" / "invoice_breakdown" / "reconciliation.xlsx"
    )

    def build(overrides):
        output = BytesIO()
        with ZipFile(source) as original, ZipFile(output, "w", compression=ZIP_DEFLATED) as updated:
            for name in original.namelist():
                updated.writestr(name, overrides.get(name, original.read(name)))
            for name, data in overrides.items():
                if name not in original.namelist():
                    updated.writestr(name, data)
        return output.getvalue()

    return build


@pytest.fixture
def task_runtime(disposable_postgres, monkeypatch, tmp_path):
    _, schema = disposable_postgres
    directory = tmp_path / "owned-sources"
    monkeypatch.setattr(settings, "source_storage_directory", directory)
    monkeypatch.setattr(settings, "azure_storage_blob_endpoint", None)
    monkeypatch.setattr(
        settings, "operator_tokens",
        SecretStr('{"alice":"' + "a" * 40 + '","bob":"' + "b" * 40 + '"}'),
    )
    original_engine = main.create_application_engine

    def engine_for_test(config):
        engine = original_engine(config)
        engine.update_execution_options(schema_translate_map={metadata.schema: schema})
        return engine

    monkeypatch.setattr(main, "create_application_engine", engine_for_test)
    monkeypatch.setattr(source_cleanup, "create_application_engine", engine_for_test)
    alembic = Config(str(ALEMBIC_INI))
    command.upgrade(alembic, "0001_database_baseline")
    command.upgrade(alembic, "head")
    return directory
