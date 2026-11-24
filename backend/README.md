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

## Database and deployment

Use [`.env.example`](.env.example) as a template when configuring PostgreSQL. The database module connects using SQLAlchemy and psycopg. `POSTGRES_SCHEMA` is reserved for future use and defaults to `knowledge_intelligence`. The current scaffold leaves the connection search path at the database session default.

The existing backend workflow publishes the `knowledge-intelligence-api` image to GHCR and checks `/health` on Azure Container Apps. See the [infrastructure guide](../infra/README.md) for configuration.
