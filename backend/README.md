# AFLUX Invoice Breakdown Agent backend

FastAPI exposes `GET /health` for liveness and `GET /ready` for database readiness. Workbook processing and persistent workflow endpoints are planned.

## Local startup

From the repository root, with Python 3.12 installed:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

## Docker startup

From the repository root, with Docker running:

```bash
docker build -t knowledge-intelligence-api:local backend
docker run --rm -p 8000:8000 knowledge-intelligence-api:local
```

## Verify startup

```bash
curl --fail --silent --show-error http://localhost:8000/health
```

Expected response with default settings:

```json
{
  "status": "healthy",
  "service": "knowledge-intelligence-api",
  "name": "AFLUX Invoice Breakdown Agent",
  "environment": "development"
}
```

The `name` field matches the title shown in the [local API docs](http://localhost:8000/docs). The `service` field retains the identifier used by the existing deployment configuration. Both startup paths can serve this response without a database URL or a local `.env` file.

## Supabase migrations and startup

From the repository root, with the existing `backend/.env` configured:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m alembic current
python -m uvicorn app.main:app --reload --port 8000
```

See [`.env.example`](.env.example) for variable names. Use a `postgresql+psycopg://` URL for Supabase's direct connection or session pooler. The migration role needs permission to create the schema on an empty database and create objects in it. Use session-mode connections because the backend sets a per-connection search path.

Alembic and the backend use the same `DATABASE_URL` and `POSTGRES_SCHEMA` settings. Alembic creates the configured schema before initializing its version table, and connections set their search path to that schema. The baseline creates no application tables. Future ORM tables should use `app.database.metadata` and refer to related tables in that same metadata.

After migration and startup, check database readiness:

```bash
curl --fail --silent --show-error http://localhost:8000/ready
```

An HTTP 200 response with `{"status":"ready"}` means the revisions recorded
in the configured schema match the repository migration heads. Missing or
invalid database settings, connection errors, or a revision mismatch return
HTTP 503 with `{"status":"not_ready"}`. Check the backend logs for the failure
category. `/health` remains available without PostgreSQL.

The backend uses a 3-second PostgreSQL connection timeout, a 2-second pool
checkout timeout, and a 2-second PostgreSQL statement timeout during the
revision check. FastAPI disposes of the connection pool on shutdown. Run
migrations explicitly before starting database-backed operations.

LangGraph checkpoint setup and workflow resume are tracked in issue #5.

## Tests

From `backend/` with the virtual environment active:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Backend CI provides a disposable PostgreSQL instance for the migration and
restart test and builds and smoke-tests the Docker image. The integration test
runs only when `TEST_DATABASE_URL` points to a disposable
`invoice_breakdown_test` database on `127.0.0.1`. It uses its own schema and a
test-only record. Keep the Supabase URL in `DATABASE_URL` for development.

## Deployment

The existing backend workflow publishes the `knowledge-intelligence-api` image to GHCR and checks `/health` on Azure Container Apps. See the [infrastructure guide](../infra/README.md) for configuration.
