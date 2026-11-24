-- Verify setup after running the backend's Alembic upgrade.
-- This query assumes POSTGRES_SCHEMA=invoice_breakdown.
SELECT to_regnamespace('invoice_breakdown') AS application_schema,
       to_regclass('invoice_breakdown.alembic_version') AS migration_version_table;
