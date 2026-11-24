import logging

import pytest
from alembic.util import CommandError
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import main
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    config = Settings(_env_file=None, database_url=None, postgres_schema="invoice_breakdown")
    monkeypatch.setattr(main, "settings", config)
    return config


@pytest.mark.parametrize(
    ("url", "schema", "reason"),
    [
        (None, "invoice_breakdown", "DATABASE_URL is missing"),
        ("invalid-dev-credential", "invoice_breakdown", "not a valid PostgreSQL URL"),
        (
            "postgresql://user:dev-credential@127.0.0.1/db",
            "invalid-schema",
            "not a valid schema name",
        ),
    ],
)
def test_health_is_independent_of_database_config(
    isolated_settings, caplog, url, schema, reason
):
    isolated_settings.database_url = url
    isolated_settings.postgres_schema = schema
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "healthy"
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert reason in caplog.text
    assert "dev-credential" not in caplog.text
    assert "dev-credential" not in response.text


@pytest.mark.parametrize(
    ("actual", "expected", "ready"),
    [
        ({"head_a", "head_b"}, {"head_b", "head_a"}, True),
        ({"head_a"}, {"head_a", "head_b"}, False),
        ({"head_a", "head_b", "extra"}, {"head_a", "head_b"}, False),
        (set(), set(), False),
    ],
)
def test_readiness_requires_exact_head_match(
    isolated_settings, monkeypatch, actual, expected, ready
):
    isolated_settings.database_url = "postgresql://test@127.0.0.1/invoice_breakdown_test"
    isolated_settings.postgres_schema = "configured_schema"
    monkeypatch.setattr(main, "repository_heads", lambda: frozenset(expected))

    def read_database_heads(_engine, schema):
        assert schema == "configured_schema"
        return frozenset(actual)

    monkeypatch.setattr(main, "database_heads", read_database_heads)
    with TestClient(main.app) as client:
        response = client.get("/ready")
    assert response.status_code == (200 if ready else 503)
    assert response.json() == {"status": "ready" if ready else "not_ready"}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OperationalError(None, None, Exception("dev-credential")), "OperationalError"),
        (CommandError("dev-credential"), "repository migration heads could not be read"),
    ],
)
def test_readiness_sanitizes_failures(isolated_settings, monkeypatch, caplog, error, reason):
    isolated_settings.database_url = "postgresql://test@127.0.0.1/invoice_breakdown_test"

    def fail(*_args):
        raise error

    method = "repository_heads" if isinstance(error, CommandError) else "database_heads"
    monkeypatch.setattr(main, method, fail)
    with caplog.at_level(logging.WARNING), TestClient(main.app) as client:
        response = client.get("/ready")
        assert client.get("/health").status_code == 200
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert reason in caplog.text
    assert "dev-credential" not in caplog.text
    assert "dev-credential" not in response.text
