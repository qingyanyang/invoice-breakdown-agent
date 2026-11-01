-- Manual Supabase setup for Enterprise Knowledge Intelligence.
-- Run once in the Supabase SQL editor (or psql) against the shared project.
-- Do NOT run via Azure Bicep.
--
-- Shared project with Enterprise AI Agent Platform:
--   - Agent Platform schema: agent_platform
--   - This app schema:       knowledge_intelligence
-- Isolation: separate schemas + separate Alembic version tables (see below).

-- 1) Application schema
CREATE SCHEMA IF NOT EXISTS knowledge_intelligence;

-- 2) pgvector (required for embeddings / hybrid retrieval)
-- On Supabase, enable the "vector" extension in Dashboard → Database → Extensions
-- if CREATE EXTENSION fails for your plan, then:
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

-- Optional: allow use from the app schema (Supabase often installs vector under extensions)
-- GRANT USAGE ON SCHEMA extensions TO postgres, anon, authenticated, service_role;

-- 3) Alembic isolation
-- Point Alembic at this schema so version history does not collide with agent_platform.
-- Example env_py / alembic.ini approach (application config, not Bicep):
--   version_table_schema = knowledge_intelligence
--   OR configure MetaData(schema="knowledge_intelligence") and
--   context.configure(..., version_table_schema="knowledge_intelligence")
--
-- Ensure search_path / POSTGRES_SCHEMA=knowledge_intelligence so app tables land here.

-- 4) Grants (adjust role names to match your Supabase DB user used in DATABASE_URL)
-- GRANT USAGE ON SCHEMA knowledge_intelligence TO postgres;
-- GRANT ALL ON SCHEMA knowledge_intelligence TO postgres;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge_intelligence
--   GRANT ALL ON TABLES TO postgres;

-- Verify:
--   SELECT nspname FROM pg_namespace WHERE nspname = 'knowledge_intelligence';
--   SELECT extname, extnamespace::regnamespace FROM pg_extension WHERE extname = 'vector';
