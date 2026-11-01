#!/usr/bin/env bash
set -euo pipefail

# Usage:
# 1. az login
# 2. Optional: export AZURE_SUBSCRIPTION_ID="<subscription-id>"
# 3. Phase 2 only:
#      export DATABASE_URL='postgresql+psycopg://...'
#      export LLM_API_KEY='...'   # optional until LLM is wired
# 4. bash scripts/infra/deploy.sh

RG="rg-knowledge-intelligence-demo"
TEMPLATE="infra/main.bicep"
PARAMS="infra/parameters/demo.bicepparam"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cd "$ROOT"

# --------------------------------------------------
# Azure subscription
# --------------------------------------------------

ACTIVE_SUBSCRIPTION_ID="$(az account show --query id -o tsv 2>/dev/null)" || {
  echo "Azure CLI is not logged in. Run: az login"
  exit 1
}

[[ -n "$ACTIVE_SUBSCRIPTION_ID" ]] || {
  echo "No active Azure subscription. Run: az login"
  exit 1
}

AZURE_SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$ACTIVE_SUBSCRIPTION_ID}"

if [[ "$ACTIVE_SUBSCRIPTION_ID" != "$AZURE_SUBSCRIPTION_ID" ]]; then
  echo "Wrong Azure subscription."
  echo "Expected: $AZURE_SUBSCRIPTION_ID"
  echo "Active:   $ACTIVE_SUBSCRIPTION_ID"
  echo "Run: az account set --subscription \"$AZURE_SUBSCRIPTION_ID\""
  exit 1
fi

echo "Azure subscription: $AZURE_SUBSCRIPTION_ID"
echo "Resource group:     $RG"

# --------------------------------------------------
# Validate deployment secrets
# --------------------------------------------------

bash infra/scripts/validate-deploy-secrets.sh "$PARAMS"

# --------------------------------------------------
# Build Bicep
# --------------------------------------------------

echo
echo "Building Bicep..."

az bicep build \
  --file "$TEMPLATE" \
  --stdout > /dev/null

# --------------------------------------------------
# Optional Phase 2 parameters
# --------------------------------------------------

EXTRA_PARAMS=()

if [[ -n "${DATABASE_URL:-}" ]]; then
  EXTRA_PARAMS+=(--parameters "databaseUrl=${DATABASE_URL}")
fi

if [[ -n "${LLM_API_KEY:-}" ]]; then
  EXTRA_PARAMS+=(--parameters "llmApiKey=${LLM_API_KEY}")
  EXTRA_PARAMS+=(--parameters "enableLlmApiKey=true")
fi

# --------------------------------------------------
# Validate deployment
# --------------------------------------------------

echo
echo "Validating deployment..."

az deployment group validate \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS" \
  ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"} \
  --output none

# --------------------------------------------------
# Preview changes
# --------------------------------------------------

echo
echo "Previewing infrastructure changes..."

az deployment group what-if \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS" \
  ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}

# --------------------------------------------------
# Confirm deployment
# --------------------------------------------------

echo
read -r -p "Deploy to $RG? Type 'yes': " confirm

if [[ "$confirm" != "yes" ]]; then
  echo "Deployment cancelled."
  exit 0
fi

# --------------------------------------------------
# Deploy
# --------------------------------------------------

echo
echo "Deploying infrastructure..."

az deployment group create \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS" \
  ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"} \
  --query '{name:name,state:properties.provisioningState}' \
  --output json

echo
echo "Infrastructure deployment completed."