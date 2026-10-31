-- Post-Alembic: grant eki_app DML on existing tables/sequences + default privileges
-- for future objects created by the admin (migration) role.
--
-- Run as pgadmin against knowledge_intelligence_db AFTER Alembic has created tables:
--   ./infra/scripts/grant-postgres-app-user-dml.sh
--
-- Re-run after migrations that create new tables if default privileges were not
-- in effect for the creating role (safe to re-run).

\set ON_ERROR_STOP on

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO eki_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO eki_app;

-- Objects created later by the current role (typically pgadmin / migrator):
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eki_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO eki_app;
