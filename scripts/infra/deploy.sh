#!/usr/bin/env bash
set -euo pipefail

# Usage:
# 1. az login
# 2. export AZURE_SUBSCRIPTION_ID="<subscription-id>"
# 3. read -rs "POSTGRES_ADMIN_PASSWORD?PostgreSQL password: "; echo
#    export POSTGRES_ADMIN_PASSWORD
# 4. bash scripts/infra/deploy.sh

RG="rg-agent-platform-demo"
TEMPLATE="infra/main.bicep"
PARAMS="infra/parameters/demo.bicepparam"

# Check Azure subscription and required password
[[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] || {
  echo "Missing AZURE_SUBSCRIPTION_ID"
  exit 1
}

[[ "$(az account show --query id -o tsv)" == "$AZURE_SUBSCRIPTION_ID" ]] || {
  echo "Wrong Azure subscription"
  exit 1
}

[[ -n "${POSTGRES_ADMIN_PASSWORD:-}" ]] || {
  echo "Missing POSTGRES_ADMIN_PASSWORD"
  exit 1
}

# 1. Build and validate
az bicep build --file "$TEMPLATE" --stdout > /dev/null

az deployment group validate \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS" \
  --output none

# 2. Preview changes
az deployment group what-if \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS"

# 3. Confirm
echo
read -r -p "Deploy to $RG? Type 'yes': " confirm
[[ "$confirm" == "yes" ]] || { echo "Cancelled."; exit 0; }

# 4. Deploy
az deployment group create \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters "$PARAMS" \
  --query '{name:name,state:properties.provisioningState}' \
  --output json