#!/usr/bin/env bash
# Fail fast when dual-path secrets are missing before az deployment group create.
#
# Usage (local):
#   ./infra/scripts/validate-deploy-secrets.sh infra/parameters/demo.bicepparam
#
# Empty defaults in .bicepparam are for CI parse/what-if evaluation only —
# this script is the gate that prevents an accidental empty-secret deploy.
# Option A: when the Container App is deployed, LLM_API_KEY is required (IaC-owned).

set -euo pipefail

PARAM_FILE="${1:-infra/parameters/demo.bicepparam}"

if [[ ! -f "${PARAM_FILE}" ]]; then
  echo "Parameter file not found: ${PARAM_FILE}" >&2
  exit 1
fi

missing=0

require_nonempty() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "${value}" ]]; then
    echo "Missing required secret/env: ${name}" >&2
    missing=1
  fi
}

require_nonempty POSTGRES_ADMIN_PASSWORD

deploy_ca=false
if grep -Eq '^[[:space:]]*param[[:space:]]+deployContainerApp[[:space:]]*=[[:space:]]*true[[:space:]]*$' "${PARAM_FILE}"; then
  deploy_ca=true
fi

if [[ "${deploy_ca}" == "true" ]]; then
  require_nonempty POSTGRES_APP_PASSWORD
  require_nonempty LLM_API_KEY
  if ! grep -Eq '^[[:space:]]*param[[:space:]]+containerImage[[:space:]]*=[[:space:]]*'\''[^'\'']+'\''' "${PARAM_FILE}"; then
    echo "deployContainerApp is true but containerImage looks empty in ${PARAM_FILE}" >&2
    missing=1
  fi
fi

if [[ "${missing}" -ne 0 ]]; then
  echo "Refusing to deploy with incomplete secrets (deployContainerApp=${deploy_ca})." >&2
  exit 1
fi

echo "Deploy secrets OK (deployContainerApp=${deploy_ca})."
