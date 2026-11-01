#!/usr/bin/env bash
# Validate required deployment configuration.
#
# Phase 1 (deployContainerApp=false):
#   No application secrets required.
#
# Phase 2 (deployContainerApp=true):
#   DATABASE_URL and containerImage are required.
#   LLM_API_KEY is optional.

set -euo pipefail

PARAM_FILE="${1:-infra/parameters/demo.bicepparam}"

if [[ ! -f "$PARAM_FILE" ]]; then
  echo "ERROR: Parameter file not found: $PARAM_FILE" >&2
  exit 1
fi

missing=0

require_nonempty() {
  local name="$1"
  local value="${!name:-}"

  if [[ -z "$value" ]]; then
    echo "ERROR: Missing required secret/env: $name" >&2
    missing=1
  fi
}

# Determine whether Container App deployment is enabled.
deploy_ca=false

if grep -Eq \
  '^[[:space:]]*param[[:space:]]+deployContainerApp[[:space:]]*=[[:space:]]*true[[:space:]]*(//.*)?$' \
  "$PARAM_FILE"; then
  deploy_ca=true
fi

if [[ "$deploy_ca" == "true" ]]; then

  echo "Container App deployment enabled."

  # Required: Supabase PostgreSQL connection string
  require_nonempty DATABASE_URL

  # Required: Docker image
  if ! grep -Eq \
    "^[[:space:]]*param[[:space:]]+containerImage[[:space:]]*=[[:space:]]*'[^']+'[[:space:]]*(//.*)?$" \
    "$PARAM_FILE"; then

    echo "ERROR: containerImage is missing or empty in $PARAM_FILE" >&2
    missing=1
  fi

  # Optional: LLM_API_KEY
  # No validation required until LLM integration is implemented.

else
  echo "Container App deployment disabled."
fi

if [[ "$missing" -ne 0 ]]; then
  echo "ERROR: Deployment validation failed (deployContainerApp=$deploy_ca)." >&2
  exit 1
fi

echo "Deployment validation passed (deployContainerApp=$deploy_ca)."
