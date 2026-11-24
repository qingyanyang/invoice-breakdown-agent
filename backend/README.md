# AFLUX Invoice Breakdown Agent backend

FastAPI exposes `GET /health` for liveness and `GET /ready` for database readiness. The task API persists source uploads and input revisions. Workbook processing is planned.

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

Alembic and the backend use the same `DATABASE_URL` and `POSTGRES_SCHEMA` settings. Alembic creates the configured schema before initializing its version table, and connections set their search path to that schema. The baseline remains intact. The next migration creates the task and input revision tables in the configured schema.

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
revision check. A 4-second client deadline covers the full database check,
including the pool pre-ping. A stalled connection is discarded. FastAPI
disposes of the connection pool on shutdown. Run
migrations explicitly before starting database-backed operations.

Run `python -m app.checkpoints` after Alembic to install or upgrade the LangGraph checkpoint tables. Both commands use the configured PostgreSQL schema. Application startup leaves these migrations to the operator.

## Task API and source storage

Configure `OPERATOR_TOKENS` as a JSON object mapping operator IDs to distinct
random URL-safe bearer tokens of at least 32 characters. Keep the values in
your private `.env` or the deployment secret. The API derives ownership from
the authenticated operator. Unconfigured authentication disables the task API.

On Azure Container Apps, sources go to the existing private Blob container
configured by `AZURE_STORAGE_BLOB_ENDPOINT` and
`AZURE_STORAGE_DOCUMENTS_CONTAINER`. The application uses the existing
user-assigned managed identity identified by `AZURE_CLIENT_ID`. If the identity
cannot write blobs, check its Blob Data Contributor assignment. Supply
`OPERATOR_TOKENS` as a protected `demo` environment secret for an approved
infrastructure deployment.

For local development, set `SOURCE_STORAGE_DIRECTORY` to a private directory.
For Docker, mount a persistent volume there if using filesystem storage.
Azure Blob takes precedence when its endpoint is configured. With neither
backend configured, source uploads and retrieval return HTTP 503.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks` | JSON with optional `target`. Creates a task at revision 1. |
| `GET /tasks/{id}` | Reads the operator's current task revision. |
| `PUT /tasks/{id}/source?expected_revision=1&filename=source.xlsx` | Sends the raw workbook bytes as the request body. A successful upload creates a new revision. |
| `GET /tasks/{id}/source` | Downloads the current source. Use `?revision=N` to retrieve that task's earlier source. |
| `PUT /tasks/{id}/target` | JSON with `expected_revision` and a `target` object or null. A changed target creates a new revision. |

All routes require an `Authorization: Bearer` header. Supply a target such as
`{"amount":"125.00","gst_basis":"unknown"}`. Use `excluding_gst` or
`including_gst` only when confirmed. The server stores the amount as a decimal,
and `unknown` remains available for later clarification. Send the latest
`expected_revision` on every change. Concurrent attempts on the same revision
produce one successful change, and stale attempts return HTTP 409. The new
revision has its own identity for future confirmations, calculations and QA.

Uploads accept `.xlsx` up to 10 MiB with up to 64 MiB of ZIP content after
expansion. The API rejects corrupt packages, encrypted workbooks and macro
content. Legacy `.xls` import is deferred and returns HTTP 415. Source validation
opens the package without executing formulas, running macros, or following
external links.

Each accepted upload is stored under a new key. Source retrieval checks its
size and SHA-256. If publication fails, cleanup checks PostgreSQL under the
task lock and deletes the object only if no revision references it. If cleanup
cannot confirm that, the service logs the storage key and leaves the object.
After recovery, use the same database and storage settings to check a logged
key or an object suspected of being left behind after a worker crash:

```bash
python -m app.source_cleanup 'sources/<task-id>/<file-id>.xlsx'
```

The command preserves objects referenced by any revision, including old
revisions. If a filesystem staging file could not be removed, pass the exact
`.staging` key from the logs to the same command. Use the original filesystem
storage and database for that cleanup.

## Start a workflow and answer a clarification

Upload a source first. Then use the operator's bearer token on these routes:

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/workflow` | Send `{"expected_revision":2}` to start or recover that revision. |
| `GET /tasks/{id}/workflow` | Read the current revision's durable status and question, if present. |
| `POST /tasks/{id}/workflow/answers` | Send the current `expected_revision`, the pending `question_id`, and `answer` equal to `ex_gst` or `inc_gst`. |

If the submitted target has an unknown GST basis, the workflow pauses with
`awaiting_clarification`. The evidence contains the submitted amount and its
unknown basis. Confirming the basis applies to this same revision. The task
input retains its original basis and the workflow shows the confirmed
`effective_gst_basis`. Use the answer endpoint for this confirmation. Changing
the target amount or replacing a source creates a new revision with a new
checkpoint thread. Old questions and answers remain bound to their original
revision.

An invalid answer returns HTTP 422. Duplicate or stale submissions return
HTTP 409 and cannot advance another revision. An accepted answer stays
recorded even if checkpoint resume is interrupted. After a restart or
HTTP 503, read the workflow status. If it shows `answer_accepted`, repeat
`POST /tasks/{id}/workflow` with that revision to resume or reconcile the
checkpoint. Concurrent recovery requests share the task lock.

This workflow stops at `ready_for_extraction`. A missing or unavailable
checkpoint store returns HTTP 503 from workflow actions. Run the explicit
checkpoint setup before enabling these actions. Workbook extraction is
tracked in issue #6.

Stop workflow traffic before downgrading to Alembic revision `0002_task_sources`.
The downgrade keeps tasks, input revisions, and source files, and removes this
workflow's questions, answers, and matching checkpoint threads. Checkpoint
tables and unrelated threads remain available. After re-upgrade and checkpoint
setup, start a retained task again to re-confirm any unknown GST basis.

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
