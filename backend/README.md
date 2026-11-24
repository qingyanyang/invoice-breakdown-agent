# AFLUX Invoice Breakdown Agent backend

The current FastAPI scaffold exposes `GET /health`. Workbook processing and persistent workflow endpoints are planned. The health endpoint checks that the service responds. Database-backed operations require `DATABASE_URL`, which the database module checks when it is loaded.

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

LangGraph checkpoint persistence has separate setup when a PostgreSQL checkpointer is introduced. The current baseline covers only Alembic.

## Deployment

The existing backend workflow publishes the `knowledge-intelligence-api` image to GHCR and checks `/health` on Azure Container Apps. See the [infrastructure guide](../infra/README.md) for configuration.
