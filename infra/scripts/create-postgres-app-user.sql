-- Phase 1.5 / pre-Alembic: CONNECT + USAGE for eki_app (no password in this file).
-- Role create/rotate SQL is generated at runtime by create-postgres-app-user.sh into a
-- mode-0600 temp file so the app password is never passed on the psql command line.
--
-- Intentionally grants ONLY CONNECT + USAGE. Table/sequence DML does not exist yet.
-- After Alembic creates application tables, run:
--   ./infra/scripts/grant-postgres-app-user-dml.sh

\set ON_ERROR_STOP on

GRANT CONNECT ON DATABASE knowledge_intelligence_db TO eki_app;
GRANT USAGE ON SCHEMA public TO eki_app;
