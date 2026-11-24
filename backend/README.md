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

## Extract source evidence

Once the current workflow revision reaches `ready_for_extraction`, send
`POST /tasks/{id}/extraction` with the operator bearer token and
`{"expected_revision":2}`. Use `GET /tasks/{id}/extraction` with the same
authorization to read the recorded outcome for the current revision.
A successful extraction can be retrieved again without re-parsing. A retry
can replace a failed outcome for the same revision and extractor version.

The result is tied to the immutable source SHA-256 and extractor version.
`result.raw` preserves sheet order and visibility, cell coordinates and types,
raw values and text, number format IDs and available codes, hidden rows and
columns, merged ranges, and cell notes and comments. Shared-formula references
retain the anchor when the source provides one. Stored array results also
retain their formula anchor and declared range. A raw formula cache is either
`missing` or `saved_unverified`. Extraction does not calculate formulas or
label saved results verified.

`result.identified` points back to raw cells for explicit workbook labels
and headers. Review unresolved metadata and ambiguous columns against the raw
evidence. Report and quotation dates retain their own labels. The original
source remains available through the task's source download endpoint.

Extraction enforces a 10 MiB uploaded and 64 MiB expanded workbook limit,
100 sheets, 50,000 stored cells, and a 16 MiB result. Structural record and
individual metadata part limits bound parsing. Source retrieval and parsing
have a 20-second deadline. A timed-out parser is canceled. A storage read
that outlasts its request keeps its worker slot until it finishes. A failed extraction
records its error with no partial result, and a changed source or revision
returns HTTP 409 before publication.

Reconciliation and classification consume this evidence.

## Reconcile source totals and scope

Use the operator's bearer token after publishing an extraction for the current
revision.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/reconciliation` | Send `expected_revision` and an optional `scope` object. Reconcile the current extraction. |
| `GET /tasks/{id}/reconciliation` | Read the report, pending questions, and confirmed decisions for the current extraction. |
| `POST /tasks/{id}/reconciliation/answers` | Send `expected_revision`, a pending `question_id`, and one of its offered answers. |

A scope object accepts `mode` equal to `source` or `base_only`. In source mode,
`include` accepts verified outside-scope detail references and `exclude`
accepts traced source references, such as `Trade Summary!D10`. Base-only mode
removes Option, Removal, and Deletion contributions. Their labels identify
scope candidates. The workbook dependency trace or an explicit source-scope
answer establishes whether the reported Total includes them, including rows
listed after a literal Total. Exclusions explicitly documented on a source
row are recorded with their provenance. The signed amount and its effect on
the bridge are stored separately. An authorized addition needs evidence or
clarification of its GST basis before it can enter the working scope. A
conflicting GST basis blocks advancement.

The control total uses explicit project-total labels and separates known
sheet subtotals, alternatives, and GST-inclusive totals. Equally plausible
controls require a choice. The supported hierarchy uses declared additive
references, vertical SUM ranges, or a clearly labelled flat table. Markup
can be checked against explicit base or quantity/rate inputs where the
source structure supports it. Unsupported calculations, overlapping
dependencies, or untraced rows remain blocked. Confirmed meanings are
recorded separately from independent arithmetic. Formula caches retain
their extraction verification status, and answers supply no substitute
amounts for missing calculation evidence.

The report includes the reported Total, the independent total, the exact
difference, and a cent-level difference of independent minus reported.
Arithmetic uses Decimal at up to 256-digit precision. Round the two totals
to cents using ROUND_HALF_UP for the gate. Round the working scope only
after applying the signed additions and exclusions. The report reaches
`ready_for_classification` only when the cent difference is zero, the source
basis and contribution evidence are established, and no unresolved
questions or issues remain. A cent-level mismatch produces a
`source_correction` question naming the control cell and the evidence needed.
That question requires a corrected source upload and a new revision.

For the synthetic reconciliation fixture, D12 references D6, D9, D10 and D11.
D6 contributes D4 (B4 × 1.10 = 110.00) and D5 (B5 × 1.10 = 55.00).
D9 contributes D7 and D8 (60.00 − 10.00). D10 adds 25.00 and D11 adds
20.00. The six detail amounts sum to 260.00. With no authorized scope
changes, the working scope is 260.00.

Source rules follow the approved specification mapped in the fixture guide,
particularly sections II, III, VI and X.

## Classify trades and areas

Set `CLASSIFICATION_SKILL_PATH` to the approved `SKILL.md` file and make the
file available to the backend. For a container, mount it read-only and use
its path inside the container. Configure `CLASSIFICATION_MODEL` with an
OpenAI model that supports strict structured outputs, and supply `LLM_API_KEY`
through the deployment secret or a private local environment. Enable the
provider only for workbook evidence the company allows it to process.
The request sends the full primary specification, current working-row
descriptions, relevant source labels and Notes. The source workbook, target
amount and operator credentials stay in the service. Optionally set
`CLASSIFICATION_CODEX_PATH` to the approved `Invoice.Breakdown.Codex.md`.
The backend checks the registered 2026-10-05 SHA-256 and sends only its
classification and area sections (IV–V). The report records whether the
supplement was applied, with its checksum and chapters when verified.
Classification can proceed from `SKILL.md` alone when the supplement is
absent or fails verification.
Run `alembic upgrade head` explicitly before enabling classification.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/classification` | Send `expected_revision` after reconciliation is ready. Starts classification or returns the current result. Retry here after addressing a failed attempt. |
| `GET /tasks/{id}/classification` | Read current assignments, evidence, pending questions, accepted answers and the SHA-256 of the primary specification. |
| `POST /tasks/{id}/classification/answers` | Send `expected_revision`, the current `question_id`, one offered `answer` and a `reason` when required. |

Classification carries forward the exact signed amount of each reconciled
row and keeps a single coverage entry per row. The code checks the model's
references and trade labels, row coverage and amount conservation.
Semantic proposals retain their rationale and provenance for review. An
owner's answer is recorded as a confirmed interpretation and carries no
new price. Complete classification reaches `ready_for_allocation`.

A priced Glazing component can be subtracted from the Partition parent
when a workbook label establishes its inclusion and both use the same
markup basis. The report records the parent, component, inclusion
reference and subtraction. Unsupported splits require clarification.
When the only source attribution covers combined floors without separate
amounts, the owner must confirm keeping the full amount on the combined
label. A model proposal that differs from the only source area label also
requires confirmation. Rows without an area go to `Additional Items`.

A conflict between an Access Panel exclusion note and a priced working
row pauses classification. If the owner requests a scope change, make it in
reconciliation before rerunning classification. Each scope or reconciliation
decision change creates a new generation, invalidating earlier answers
and assignments even if the task revision stays the same. Source and
target revisions also invalidate prior classification. Pending answers
can resume from the persisted proposal after restart. Classifier upgrades
that change the version require a fresh classification for the current
reconciliation.

An unavailable provider, invalid proposal or missing primary specification
records a failed outcome. The request supports up to 300 working rows and
192 KiB of model input evidence. Limit failures stop the stage before
publication. Tests use synthetic data and a simulated model.

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
