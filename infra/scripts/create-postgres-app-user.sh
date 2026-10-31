#!/usr/bin/env bash
# Create or rotate the least-privilege eki_app role without writing a password into git
# and without putting secrets on the psql command line (visible in process listings).
#
# Required env:
#   POSTGRES_HOST            Flexible Server FQDN
#   POSTGRES_ADMIN_PASSWORD  Server admin password (for this connection only)
#   POSTGRES_APP_PASSWORD    Password assigned to eki_app
#
# Optional:
#   POSTGRES_ADMIN_USER      default pgadmin
#   POSTGRES_DB              default knowledge_intelligence_db
#   POSTGRES_PORT            default 5432
#
# Secrets handling (MVP):
#   - Admin auth: temporary PGPASSFILE (mode 0600), not argv / long-lived PGPASSWORD
#   - App password: temporary SQL file (mode 0600) with dollar-quoted literal, not psql -v
#   - Temp dir is deleted on exit
#
# Grants CONNECT + USAGE only. After Alembic creates tables, also run:
#   ./infra/scripts/grant-postgres-app-user-dml.sh
#
# Example:
#   export POSTGRES_HOST='...'
#   export POSTGRES_ADMIN_PASSWORD='...'
#   export POSTGRES_APP_PASSWORD='...'
#   ./infra/scripts/create-postgres-app-user.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT_SQL="${SCRIPT_DIR}/create-postgres-app-user.sql"

: "${POSTGRES_HOST:?Set POSTGRES_HOST to the Flexible Server FQDN}"
: "${POSTGRES_ADMIN_PASSWORD:?Set POSTGRES_ADMIN_PASSWORD (admin login only; not used by the app)}"
: "${POSTGRES_APP_PASSWORD:?Set POSTGRES_APP_PASSWORD (runtime eki_app password)}"

POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-pgadmin}"
POSTGRES_DB="${POSTGRES_DB:-knowledge_intelligence_db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

if [[ ! -f "${GRANT_SQL}" ]]; then
  echo "SQL file not found: ${GRANT_SQL}" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required (install PostgreSQL client tools)." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to write protected temp credentials/SQL." >&2
  exit 1
fi

umask 077
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eki-pg-app-user.XXXXXX")"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

PGPASS_FILE="${TMP_DIR}/pgpass"
ROLE_SQL="${TMP_DIR}/create-role.sql"

# Write admin .pgpass + role SQL. Secrets are not passed as psql -v / CLI args.
POSTGRES_HOST="${POSTGRES_HOST}" \
POSTGRES_PORT="${POSTGRES_PORT}" \
POSTGRES_DB="${POSTGRES_DB}" \
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER}" \
POSTGRES_ADMIN_PASSWORD="${POSTGRES_ADMIN_PASSWORD}" \
POSTGRES_APP_PASSWORD="${POSTGRES_APP_PASSWORD}" \
PGPASS_FILE="${PGPASS_FILE}" \
ROLE_SQL="${ROLE_SQL}" \
python3 <<'PY'
import os
import secrets

def escape_pgpass(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")

host = os.environ["POSTGRES_HOST"]
port = os.environ["POSTGRES_PORT"]
db = os.environ["POSTGRES_DB"]
user = os.environ["POSTGRES_ADMIN_USER"]
admin_password = os.environ["POSTGRES_ADMIN_PASSWORD"]
app_password = os.environ["POSTGRES_APP_PASSWORD"]
pgpass_path = os.environ["PGPASS_FILE"]
role_sql_path = os.environ["ROLE_SQL"]

with open(pgpass_path, "w", encoding="utf-8") as pgpass:
    pgpass.write(
        ":".join(
            [
                escape_pgpass(host),
                escape_pgpass(port),
                escape_pgpass(db),
                escape_pgpass(user),
                escape_pgpass(admin_password),
            ]
        )
        + "\n"
    )
os.chmod(pgpass_path, 0o600)

tag = "p" + secrets.token_hex(16)
while f"${tag}$" in app_password:
    tag = "p" + secrets.token_hex(16)
quoted = f"${tag}${app_password}${tag}$"

# Embed app password only in this mode-0600 file (dollar-quoted), never on argv.
role_sql = f"""\\set ON_ERROR_STOP on

DO $do$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'eki_app') THEN
    EXECUTE format('CREATE ROLE eki_app LOGIN PASSWORD %L', {quoted});
  ELSE
    EXECUTE format('ALTER ROLE eki_app WITH PASSWORD %L', {quoted});
  END IF;
END
$do$;
"""

with open(role_sql_path, "w", encoding="utf-8") as role_sql_file:
    role_sql_file.write(role_sql)
os.chmod(role_sql_path, 0o600)
PY

# Prefer PGPASSFILE so admin secret is not kept in PGPASSWORD for the psql process env.
unset PGPASSWORD || true
export PGPASSFILE="${PGPASS_FILE}"

CONNINFO="host=${POSTGRES_HOST} port=${POSTGRES_PORT} dbname=${POSTGRES_DB} user=${POSTGRES_ADMIN_USER} sslmode=require"

psql "${CONNINFO}" -v ON_ERROR_STOP=1 -f "${ROLE_SQL}"
psql "${CONNINFO}" -v ON_ERROR_STOP=1 -f "${GRANT_SQL}"

echo "Role eki_app created/updated on ${POSTGRES_DB}@${POSTGRES_HOST} (password not logged)."
