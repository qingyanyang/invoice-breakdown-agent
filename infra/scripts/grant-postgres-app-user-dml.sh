#!/usr/bin/env bash
# Grant eki_app table/sequence DML after Alembic has created the application schema.
# Run as the PostgreSQL admin. Does not need POSTGRES_APP_PASSWORD.
#
# Required env:
#   POSTGRES_HOST
#   POSTGRES_ADMIN_PASSWORD
#
# Optional:
#   POSTGRES_ADMIN_USER  default pgadmin
#   POSTGRES_DB          default knowledge_intelligence_db
#   POSTGRES_PORT        default 5432
#
# Order:
#   1. ./infra/scripts/create-postgres-app-user.sh   # role + CONNECT/USAGE
#   2. alembic upgrade head                         # as admin
#   3. ./infra/scripts/grant-postgres-app-user-dml.sh
#   4. Start / deploy FastAPI with eki_app credentials

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/grant-postgres-app-user-dml.sql"

: "${POSTGRES_HOST:?Set POSTGRES_HOST to the Flexible Server FQDN}"
: "${POSTGRES_ADMIN_PASSWORD:?Set POSTGRES_ADMIN_PASSWORD (admin login only)}"

POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-pgadmin}"
POSTGRES_DB="${POSTGRES_DB:-knowledge_intelligence_db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

if [[ ! -f "${SQL_FILE}" ]]; then
  echo "SQL file not found: ${SQL_FILE}" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required (install PostgreSQL client tools)." >&2
  exit 1
fi

export PGPASSWORD="${POSTGRES_ADMIN_PASSWORD}"

psql \
  "host=${POSTGRES_HOST} port=${POSTGRES_PORT} dbname=${POSTGRES_DB} user=${POSTGRES_ADMIN_USER} sslmode=require" \
  -v ON_ERROR_STOP=1 \
  -f "${SQL_FILE}"

echo "Granted table/sequence DML on ${POSTGRES_DB} to eki_app."
