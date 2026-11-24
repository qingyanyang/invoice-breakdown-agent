# AFLUX Invoice Breakdown Agent

A headless, single-company backend for creating AFLUX Invoice Breakdown workbooks from project spreadsheets. The creation MVP will reconcile the source workbook, classify its amounts by trade and area, and produce a new workbook with traceable calculations. When an authorized target contract amount is supplied, the workflow will verify its GST basis and adjust eligible amounts while preserving protected amounts.

The current scaffold exposes a FastAPI health endpoint and includes PostgreSQL connection setup and Docker packaging. Workbook uploads, processing, and persistent execution are planned.

## Workflow goals

- Preserve the source workbook and trace extracted values to their original sheets and cells.
- Reconcile the source control total to the authorized scope and pause for material clarification.
- Use model reasoning to interpret source structure and propose classifications. Enforce prerequisites, calculate monetary amounts with Decimal, and validate totals in code.
- Persist task revisions, file references, clarifications, and LangGraph checkpoints in PostgreSQL.
- Create a separate workbook and record which formula and layout checks were performed before delivery.

The planned backend stack is Python, FastAPI, LangGraph, PostgreSQL or Supabase, Alembic, pytest, and Docker. The creation MVP is internal and API-first. Revisions, comparisons, approved-rule retrieval, and one chat integration are follow-on capabilities. Tax invoices, quotations, bookkeeping, payment reconciliation, and payment claims are outside the product scope.

## Run locally

See the [backend setup](backend/README.md) for Python and Docker startup. Both support running the health endpoint without database credentials.

- [Backend](backend/) contains the FastAPI service and PostgreSQL connection setup.
- [Infrastructure](infra/) contains the existing Azure deployment scaffold.
- [Frontend](frontend/) contains the existing React scaffold. The headless MVP runs independently of it.
