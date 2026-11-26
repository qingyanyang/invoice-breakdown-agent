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
checkpoint. Each checkpoint thread uses a PostgreSQL session advisory
lock on the checkpointer connection. A replacement worker resumes from
the saved snapshot after the original session releases that lock.

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
its path inside the container. `CLASSIFICATION_SKILL_SHA256` defaults to
the approved workflow checksum in the fixture guide. Set it explicitly
when deploying a newly approved copy. `CLASSIFICATION_SKILL_VERSION` is
an optional approved version label. Without a label the checksum is the
version identifier. Configure `CLASSIFICATION_MODEL` with an
OpenAI model that supports strict structured outputs, and supply `LLM_API_KEY`
through the deployment secret or a private local environment. Enable the
provider only for workbook evidence the company allows it to process.
The request sends the full operational primary specification, current working-row
descriptions, relevant source labels and Notes. The source workbook, target
amount and operator credentials stay in the service. Optionally set
`CLASSIFICATION_CODEX_PATH` to the approved `Invoice.Breakdown.Codex.md`.
The backend checks the registered 2026-10-05 SHA-256 and sends only its
classification and area sections (IV–V). The report records whether the
supplement was applied, with its checksum and chapters when verified.
A task can start from `SKILL.md` alone when the supplement is absent or
fails verification. A task that pinned a supplement requires that version
for each new classification.
Run `alembic upgrade head` explicitly before enabling classification.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/classification` | Send `expected_revision` after reconciliation is ready. Starts classification or returns the current result. Retry here after addressing a failed attempt. |
| `GET /tasks/{id}/classification` | Read current assignments, evidence, pending questions, accepted answers and the SHA-256 of the primary specification. |
| `POST /tasks/{id}/classification/answers` | Send `expected_revision`, the current `question_id`, one offered `answer` and a `reason` when required. |
| `GET /tasks/{id}/rules` | Read the task's pin or `null` before its first verification. Use `?revision=N` for an earlier revision. |
| `POST /tasks/{id}/rules/upgrade` | Send `expected_revision` and the configured approved `primary_sha256` to change the rules in a new task revision. Omit `supplementary_sha256` to retain the existing selection, supply the configured and verified checksum to select it, or pass `null` for primary-only classification. Supports an optional `Idempotency-Key`. |

### Pin and retrieve approved rules

Classification pins the verified primary checksum and any verified
supplement the first time it loads approved rules for a task. The pin
persists through retries, service restarts and ordinary task revisions.
Keep pinned versions in a private, read-only rules archive configured
with `CLASSIFICATION_RULES_ARCHIVE`. When the configured files cannot
satisfy a pin, the service checks `<archive>/<primary-sha256>/SKILL.md`
and `<archive>/<supplementary-sha256>/Invoice.Breakdown.Codex.md`.
It verifies each document against its own pinned checksum. A missing or
mismatched pinned document blocks new classification. Restore that
version or explicitly change the rule selection.
An existing published classification remains readable. Owner answers
can use its persisted proposal and provenance after a restart.
When legacy results document mixed rule versions, explicitly select the
primary and supplementary rules through the upgrade endpoint.

Upgrading is an owner action that verifies the requested checksum against
the configured approved version and creates a new revision. The new
revision references the same immutable source bytes and uses the existing
workflow gates. Earlier classification, allocation, workbook and QA
records remain attached to their original revisions. To classify and
deliver under the new pin, run the normal workflow for the new revision.
Apply Alembic migration `0013_task_rule_pins` via `alembic upgrade head`.

Rule search uses deterministic phrase matching and a fixed, versioned
passage map. It selects up to six approved passages from the exact
verified primary document, using current row descriptions, area labels,
and reconciled scope cues. Each passage records its document, approved
version, checksum, section, immutable ID, exact excerpt, and source
references where the search matched. The service verifies and loads the full
pinned operational file for every new model proposal. Historical examples
live in evaluation fixtures that the application never reads. Keep literal
project amounts in authorized current workbooks. The operational rules loader
rejects evaluation blocks and nonzero currency amounts, while preserving
the approved zero-difference rule and monetary formatting instructions.
A mixed primary file blocks classification even when its checksum matches.
A mixed supplement is withheld, and classification blocks if the task
requires that pinned version. If optional search finds no match, encounters
a version mismatch, or is unavailable, classification uses the same verified
operational rules. The execution fingerprint records the primary checksum,
the supplementary version actually sent, and the retrieval configuration
version. Current-project scope and amounts still follow the existing
source evidence and deterministic checks.

The focused retrieval suite tests section relevance, scope and combined
areas, no match, version isolation, and exclusion of historical example
amounts. Integration tests use synthetic workbooks, disposable PostgreSQL,
and a mocked model. They verify that mixed rules fail before search or a
provider request, that the full verified operational rules reach the
provider, and that an unsupported model claim is rejected before it can be
stored. A current authorized amount identical to a historical example is
preserved.
Real-model behavioral evaluation of rule application and historical-data
contamination remains unperformed.


Classification carries forward the exact signed amount of each reconciled
row and keeps a single coverage entry per row. The code checks the model's
references and trade labels, row coverage and amount conservation.
The review rationale quotes a cited current source cell and identifies the
model's proposed trade or request for clarification. Cached reports and
historical receipts omit legacy, model-written rationales. Recorded
assignments, amounts and owner decisions remain available. An
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

## Allocate an authorized target

Run the current classification until it returns `ready_for_allocation`.
Use its `proposal_id` with the current revision to approve allocation.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/allocation` | Send `expected_revision` and `expected_classification_id`. Runs allocation using the target and GST approval stored on the task. |
| `GET /tasks/{id}/allocation` | Read the report for the current allocation inputs and approvals. |

An optional `policy` supports `protected_rows` (working-scope row references),
`rounding_preference` (a displayed `area` and `trade`), and
`requested_direction` (`increase` or `decrease`). To change a previously
recorded policy, submit its `allocation_id` as `expected_allocation_id`.
Repeating the current policy returns the recorded result. A stale revision,
classification, or policy approval returns HTTP 409.

The primary rules are `SKILL.md` §§8–9 and 11. Working total **W** is the
exact sum of the reconciled signed assignments, including all authorized
additions. **P** is the exact sum of protected components within **W**.
The adjustable base is **W − P**, and the uniform factor is
**(T − P) / (W − P)** for approved ex-GST target **T**. Protected additions
are recorded by origin. They enter the formula once through **W** and **P**.
Default protection applies to Make Good and Access Panel. A protected
row keeps its exact source amount, including in a mixed display cell.

The allocation uses 512 significant decimal digits. Protected and adjustable
subtotals in each area/trade cell are independently rounded to cents using
ROUND_HALF_UP. The adjustable rounding uses the exact quotient of the factor's
numerator and denominator to resolve ties. The maximum legitimate target
residual is half a cent multiplied by the number of nonzero rounded components.
The exact total must already reconcile before a residual can be applied.
Use an eligible requested cell for a nonzero residual, or try the largest
adjustable magnitude first, with ties ordered by area label and trade name.
If no single cell can absorb it in automatic mode, distribute it in that
order and record the amount applied to each recipient. Positive residuals
first cover any negative displayed trade totals. Each adjustment changes
only the adjustable display component and must preserve its sign or bring
it to zero. A zero residual needs no recipient. Area, trade and grand totals
all derive from the same displayed cells. A targeted result requires a
displayed difference of `0.00`.

An unconfirmed GST basis returns a workflow prerequisite. A target that needs
a verified GST conversion, cannot cover protected amounts, or cannot be
achieved under the current rules produces a blocked report with its cause.
An increase pauses for scope review if scaling the adjustable credits
would increase their magnitude by more than the entire authorized
adjustable target. The blocked report records the proposed factor and
source credit evidence. An opposite requested direction is recorded as a warning. A task with no
target records `no_contract_price_adjustment` and a null factor. Its exact
assignments pass through, and any two-decimal display discrepancy is recorded
under source presentation rounding without applying a contract adjustment.

Run `alembic upgrade head` to install the allocation table. The report
persists the approved inputs, protection provenance, factor, component
amounts and rounding checks for workbook generation.

## Generate an owner-review workbook

Run `alembic upgrade head` and obtain an allocation with status
`ready_for_workbook`. Workbook output uses the existing Azure
`AZURE_STORAGE_PROCESSED_DOCUMENTS_CONTAINER` (default `processed-documents`)
and managed identity, or the configured persistent local storage directory
under `outputs/`.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/workbook` | Send `expected_revision` and `expected_allocation_id`. Generates a workbook for owner review. |
| `GET /tasks/{id}/workbook` | Read the current artifact version, checksum, source-copy report and QA status. |
| `GET /tasks/{id}/workbook/download` | Download the current checksummed XLSX with `X-Workbook-Status: generated_awaiting_qa`. |

The task owner can retry generation for the same revision, source checksum,
allocation ID, generator version and template version. Identical requests
converge on one available artifact. Reuse verifies the stored size and
checksum. A changed input, missing object, or corrupted object creates a
new version with a unique storage key and preserves earlier manifests.
Publication rechecks the authorized inputs under the task lock and verifies
the stored bytes before recording the artifact. Failed writes expose no
download.

If the logs report an output object or local staging file pending cleanup,
run the cleanup command from the backend with that exact storage key.

```bash
python -m app.source_cleanup 'outputs/<task-id>/<artifact-id>.xlsx'
```

The command uses the output backend and checks every workbook manifest
for a reference while holding the task lock. It also accepts logged local
`.staging` keys.

The generator follows `SKILL.md` §§4, 7, 9 and 10. The summary uses the
allocation's displayed area and trade amounts, with Excel formulas for
totals. It includes confirmed project information and the actual GST basis.
Targeted workbooks include Adjustment Detail with exact source amounts
and adjustment ratios. Adjustable rows show their exact calculation.
No-target workbooks show the formula for the difference between their
displayed total and the reconciled source amount, including any nonzero
source-rounding difference.

Source integrity and source-sheet fidelity are checked independently.
The input is verified by its original checksum. Copied worksheet parts
retain cell evidence, sheet order and visibility, styles, row and column
layout, merged ranges and supported cell annotations. Source Evidence maps
original names and coordinates to the copied tabs. A limited set of
same-sheet additive and markup formulas remains active only when every
dependency survives. Other source formulas, links and defined names are
recorded as text. Long evidence continues on numbered rows that repeat its
source reference. A saved empty string remains distinct from a missing
cache. Available source caches remain unverified. The output contains no
live external relationships.

Transitional XLSX worksheets support local conditional formatting with
supported rules and operands. Charts, pivot tables, drawings other than
legacy notes, rules with unsupported dependencies, and unsupported
worksheet extensions block generation when encountered. The
API identifies the sheet and feature. The workbook is marked
`generated_awaiting_qa`. Recalculation, visual certification and final
customer delivery authorization belong to Issue #11.

## Verify and release a workbook

Run `alembic upgrade head` before starting the service. Configure a trusted
local Docker runtime and build the workbook QA image on that host.

```bash
docker build -t invoice-breakdown-qa:local backend/qa_worker
```

Set `QA_WORKER_IMAGE=invoice-breakdown-qa:local` and optionally provide
the Docker executable as `QA_DOCKER_RUNTIME`. QA needs access to that
local runtime. Each job runs without network or host mounts, with a
read-only root, an unprivileged user, and limits on elapsed time,
temporary storage, memory, CPU, and processes. The worker uses a fresh
LibreOffice profile with macro execution disabled for the untrusted
workbook. The API rejects live external relationships before calling it.
An unavailable or timed-out tool records an unperformed check.

| Method and path | Request and result |
| --- | --- |
| `POST /tasks/{id}/delivery/qa` | Send `expected_revision` and `expected_artifact_id` from the current `/workbook` response. Runs or resumes QA for those exact inputs. |
| `GET /tasks/{id}/delivery/qa` | Read the recorded outcomes, evidence, sheet ranges, and candidate and preview checksums. |
| `GET /tasks/{id}/delivery/qa/preview` | Download the candidate's PDF proof. The QA report maps each page to its worksheet and populated range. |
| `POST /tasks/{id}/delivery/qa/visual-review` | Submit `expected_revision`, `expected_artifact_id`, `expected_qa_id`, `candidate_sha256`, `preview_sha256`, and one sheet result per preview. |
| `GET /tasks/{id}/delivery/download` | Download the exact approved candidate after all required checks pass. The ready QA response includes a Chinese delivery summary and this link. |

A sheet result has a `sheet` name and `outcome` of `passed` or `failed`.
Check layout, clipping, text, dates, and number formats before marking each
sheet passed. Include a `note` for failures. The review is bound to both
checksums and the current run. The owner can still download Issue #10's
`generated_awaiting_qa` review workbook from `/workbook/download`.

QA clears formula caches on a disposable copy to obtain fresh results.
It imports only results for matching formula coordinates and expressions
into the original workbook package. It then removes the review banner
from the candidate, verifies that all other worksheet content and layout
still match, and replays scope and allocation against candidate amounts.
Each source contributor, authorized addition, subtotal, and control total
must match its independent calculation at the reconciliation's cent
precision. Actual formula errors and nonzero displayed differences also
block release. The versioned candidate is stored separately.

The PDF is rendered from the candidate with hidden sheet, row, and column
content exposed on a disposable preview copy. A separate layout pass
measures the cell rectangles. Expected text must be painted in its own
cell region within the page and clipping bounds. Unsupported formats,
missing coverage, and clipped content prevent approval. The owner still
reviews readability and layout on the candidate's preview.

When rendering is temporarily unavailable, a retry resumes from the saved
candidate if the worker identity agrees. A changed worker version or
image restarts recalculation and rendering. Changes to the workbook,
candidate, or QA checks require current evidence and owner approval.

## Execution, idempotency and recovery

Run the explicit migration to `0012_execution_recovery` before using
these routes. Keep the PostgreSQL direct or session-mode connection for
checkpoint locks. The existing separate checkpoint setup still applies.

Long-running mutation stages claim an execution in PostgreSQL before
doing work. A claim binds the authorized operator, task revision and
effective inputs. The lease has its own worker token and increasing
attempt number. Stages recheck the lease, source identity and applicable
upstream approvals before publishing. The application result and
execution outcome commit in one short transaction.

Repeat the same stage POST after a transient failure or process restart.
The service admits one attempt per request. Equivalent submissions wait
briefly for an active attempt and can receive HTTP 202 with `Retry-After`.
An expired lease can be reclaimed within the original execution deadline
and attempt limit. Expired workers cannot record success or failure. The
execution history uses the database clock to show whether a lease or
attempt elapsed and whether the run exhausted its budget, including on
historical revisions. Exhaustion returns HTTP 503 with a failure code.
Stage failures remain available from the existing stage GETs when a stage
result was published. Use the owner-only
`GET /tasks/{id}/executions?revision={revision}` to inspect recent attempts,
lease and deadline timestamps, why an execution stopped, and the published
result identifiers. The optional
revision defaults to the current one. `X-Execution-Id` identifies the
operation on pending and execution failure responses.

The defaults are three attempts with exponential backoff starting at
0.5 seconds, a renewable 30-second lease, and a 720-second overall
deadline. Stage time limits include queueing after a work claim. Limits
are configurable with the `EXECUTION_*` variables in `.env.example`.
Storage SDK and model HTTP requests each use one transport attempt for
a call. Application retries use the recorded budget. The native worker
keeps its local capacity slot until it exits, including after a request
timeout. A clarification, rejected input, or failed QA does not schedule
an automatic retry. The accepted answer starts a new checkpoint phase
with its own budget, regardless of how long the owner took to answer.
Visual review is a separate authorized action.

Send an optional `Idempotency-Key` on task creation, upload, target
updates, reconciliation and allocation changes, clarification answers,
and visual review. A key is scoped to the operator and operation, can use
1 to 128 visible ASCII characters, and is stored as a digest. Validated
payloads, defaults, normalized monetary amounts, and upload byte
checksums are part of the request fingerprint. Reusing a key with
different input returns HTTP 409. Reconciliation requests waiting for a
duplicate keep their original generation. An unfinished keyed request
returns HTTP 409 if that generation changed, including after a restart.
A successful mutation replays its stored receipt, including its original
revision where applicable.
An accepted answer whose checkpoint resume was interrupted replays
the acceptance receipt. Repeat the workflow POST to recover it.
Visual approval also requires the current revision and artifact on replay.
Receipts are retained for the life of the database. Reuse the original
key for retries and use a different key for a new mutation.

Recovery at each persistence boundary:

| Boundary | Recovery |
| --- | --- |
| Application commit succeeded but the response was lost | Replay the original key or repeat the stage with the same current inputs. The receipt or published result is reused. |
| The answer committed before LangGraph advanced | The workflow shows `answer_accepted`. Repeat the workflow POST on the same revision. |
| LangGraph wrote nodes or a checkpoint before the application result committed | The next worker checks the saved snapshot and applies any remaining work under the same checkpoint session lock. A completed checkpoint is reconciled into the application record. |
| Storage wrote an object before the application manifest committed | A replacement uses its own keys. Native write outcomes are recorded even when the HTTP request times out. Cleanup checks the attempt, write outcome, and each applicable manifest. |
| A QA candidate was stored and a required tool was temporarily unavailable | Retry within the current budget. Reuse the verified candidate when the worker identity agrees, or recalculate and render when it changes. Approval binds to the current candidate and preview. |

An operator can retry orphan cleanup after a crash with
`python -m app.source_cleanup --execution-id <execution-id>` or pass a
specific key from the logs. Cleanup retains active attempts, committed
references, collisions, and registered objects whose write ownership
could not be confirmed. A timed-out native write can finish after the
HTTP response. Once its outcome is recorded, rerun cleanup for any
late, confirmed write. Reservations from earlier versions have no
recorded ownership proof and are treated as unconfirmed.

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

## Slack DM demo

Issue #14 supports private one-to-one conversations with the bot in a single
test workspace. Use synthetic workbooks only. The adapter accepts an uploaded
workbook only when its SHA-256 is in the configured demo allowlist. The Slack
app uses existing operator identities and the same task ownership, revision,
and QA checks as the HTTP API.

### Test workspace setup

1. Run the backend with its existing database, checkpoint, source storage,
   classification, and QA worker settings. Apply `alembic upgrade head`
   and initialize LangGraph checkpoints as documented above.
2. Create a Slack app in the test workspace. Enable its App Home Messages
   tab for sending messages, and give its bot these scopes:
   `chat:write`, `im:history`, `im:read`, `files:read`, and `files:write`.
   Install it in the test workspace. Reinstall after changing scopes.
3. Supply `SLACK_ENABLED=true`, `SLACK_TEAM_ID`, `SLACK_APP_ID`,
   `SLACK_SIGNING_SECRET`, and `SLACK_BOT_TOKEN` through private
   environment or deployment settings. Set `SLACK_OPERATOR_MAP` to a JSON
   object such as `{"U0123456789":"operator-a"}`. Each Slack user maps to a
   distinct existing name in `OPERATOR_TOKENS`.
4. Set `SLACK_DEMO_SOURCE_SHA256` to a space-separated list of approved
   synthetic workbook checksums. For example, from the repository root run
   `shasum -a 256 tests/fixtures/invoice_breakdown/reconciliation.xlsx`
   and use the checksum column. Verify that the test deployment starts with
   the configured bot token and workspace.
5. Enable Event Subscriptions with the bot event `message.im`. Set the
   request URL to the deployed HTTPS endpoint `/integrations/slack/events`.
   Enable Interactivity and use the same HTTPS endpoint as its request URL.
   Slack signs both event and button requests with the app signing secret.

### Run a synthetic workspace test

1. Open the bot's Messages tab. Upload one allowlisted `.xlsx` in a new
   DM message. Leave the caption blank for a no-target run, or enter
   `breakdown target 282.54` to exercise GST clarification. Use
   `breakdown target 282.54 ex_gst` when the basis is known.
2. Answer using the buttons in that upload's thread. A question that
   requires supporting context provides an `answer <control> <reason>`
   reply format in the same thread.
3. When automated QA passes, open the complete QA preview PDF. Review the
   listed pages for every worksheet and record each worksheet's outcome
   using its buttons. Test that a duplicate click or a reply in another
   thread cannot advance the task.
4. Confirm that final delivery appears in the original thread after every
   required check and approval. Download the workbook and compare its
   `shasum -a 256` with the candidate checksum in the owner QA response.
   Check the Chinese summary and verify the final reconciliation difference.
5. When the bot reports a retryable failure or `delivery_unknown`, send
   `retry` in the original upload thread. The latter checks the stored
   Slack file ID. If its destination and share remain unconfirmed, ask the
   test workspace administrator to inspect that original upload.

Delivery of previews and verified workbooks is limited to the authenticated
requester in the original DM thread. Group messages and external workspaces
are excluded. Slack is disabled by default. The app stores normalized
events, thread bindings, review controls, and file IDs in PostgreSQL. One
worker per deployment processes events across replicas. PostgreSQL
serializes the worker while workflow stages run outside the inbox
transaction. A lost file completion response never causes an automatic
second upload. Set up access to the test database and Slack workspace
according to their synthetic data retention policies.

Run the automated tests with
`pytest tests/test_slack_dm.py` from `backend` using disposable PostgreSQL.
The Slack HTTP responses and model proposal are mocked. The end-to-end
test also uses the repository's isolated LibreOffice QA worker fixture.
Those tests exercise a different boundary from the live Slack workspace
test above.
