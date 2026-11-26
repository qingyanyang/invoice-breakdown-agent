# Invoice Breakdown backend architecture

The backend is organized around the stages of an Invoice Breakdown. Each
feature owns its commands, persisted results, and business rules. Split a
module when it has a separate responsibility with a useful interface.

| Module | Responsibility |
| --- | --- |
| `app.features.tasks` | Owner-scoped tasks and revisions, immutable uploads, rule pins, and execution history |
| `app.features.extraction` | Read workbook structure and source evidence. Own source coordinates and labels |
| `app.features.reconciliation` | Reconcile totals, hierarchy, markup, and scope. Own reconciliation questions and decisions |
| `app.features.classification` | Validate proposals, apply approved rules, classify source rows, and retain classification decisions |
| `app.features.allocation` | Allocate the authorized target with deterministic Decimal calculations and rounding |
| `app.features.workbooks` | Generate and record owner-review workbooks, preserving source data |
| `app.features.qa` | Run QA, record evidence, accept visual approval, and release the verified bytes |
| `app.workflows` | Own the GST confirmation graph and coordinate calls to the stage use cases |
| `app.channels.slack` | Verify Slack events, process the inbox, render controls, and recover Slack file delivery |

## Dependency rules

- Calculation and evidence modules take source data and rules as inputs.
  They do not import HTTP routes, database sessions, or external clients.
  Shared monetary operations live in `app.money`. The extraction feature
  owns source coordinates, and classification owns the trade vocabulary.
- HTTP routes validate transport input and authenticate the operator. The
  Slack worker verifies the signed event, current operator mapping, and DM
  recipient. Both pass the authenticated operator and resources explicitly
  to the same use cases. Ownership, revision, and approval checks run
  within those use cases.
- `app.outcomes.invoke` returns a success value or a business rejection.
  Exceptions inside a use case allow the owning transaction to roll back.
  Only the HTTP adapter turns pending execution into a 202 response and
  sets `Retry-After`. Slack schedules the same outcome in its durable
  inbox. File results carry bytes and status; each adapter supplies its
  own delivery representation.
- The workflow pipeline calls stages in order and stops at the first
  pending or blocked stage. Each HTTP stage can still be called explicitly.
  LangGraph owns the existing GST interrupt and recovery, while
  reconciliation and classification own the rules and evidence for their
  own questions. The stages share revision checks, receipt deduplication,
  execution claims, and fencing. Reviewed choices share question lookup
  and evidence recording.
- Keep genuinely shared infrastructure in `app.database`,
  `app.execution`, `app.request_receipts`, `app.delivery_guard`,
  `app.source_storage`, and `app.worker_io`. Feature tables share one
  SQLAlchemy metadata object. Keep transaction and storage publication
  ordering in the feature that owns the operation.

## Compatibility anchors

The application entry point remains `app.main:app`. The checkpoint setup
and orphan cleanup commands remain `python -m app.checkpoints` and
`python -m app.source_cleanup`. Alembic registers the feature tables
through its environment; the migration revisions and configured schema
continue to govern the database.

Keep graph node names, thread identifiers, persisted state fields,
execution and receipt identities, generator versions, storage keys, and
checksums stable during structural changes. Visual approval and final
delivery continue to refer to the current revision and exact QA candidate.

The approved `SKILL.md` is primary, and the approved
`Invoice.Breakdown.Codex.md` is supplementary. Runtime configuration
and rule archive paths continue to resolve the registered files.

When moving code, run the backend suite with disposable PostgreSQL and
isolated storage. Compare the API and schema contracts against the base
branch, and resume a checkpoint created before the move when changing the
workflow modules.
