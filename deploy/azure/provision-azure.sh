#!/usr/bin/env bash
# Provision OPPORTUNITIES ON THE GO on Azure. Idempotent: run it again any time; existing
# resources are reused and the images GitHub Actions deployed are kept. Works in Azure Cloud
# Shell and in Git Bash on Windows (az / gh / python on PATH). Nothing is built locally.
#
# Order (fast + required-by-CI first, slow last):
#   1. resource group, container registry, pull identity
#   2. GitHub OIDC deployer (Entra app + federated credential + roles) and the GitHub repository
#      secrets / variables - verified with `gh` before anything slow starts
#   3. Log Analytics (daily cap), VNet, Azure Files share, Container Apps environment (ARM)
#   4. PostgreSQL Flexible Server (cheapest Burstable SKU that has capacity, VECTOR allow-listed)
#   5. container apps (ARM template): redis always; api / workers / frontend once the images
#      exist in the registry. Fresh subscription: run, push (GitHub builds), run again.
#
# Required environment (keep in a password manager - ENCRYPTION_KEYS and JWT_SECRET must never
# change): POSTGRES_ADMIN_PASS APP_DB_PASS REDIS_PASS ENCRYPTION_KEYS JWT_SECRET
# Use letters / digits / - _ for the three passwords (they pass through az's command line).
# Optional: RESOURCE_GROUP [ootg-rg] LOCATION [eastasia] PREFIX [ootg] GITHUB_REPO [git remote]
#   PG_SKUS ["Standard_B1ms Standard_B2s"]  MAX_BROWSERS [2]  MAX_HITL_BROWSERS [1]
#   CUSTOM_CONSOLE_URL  SKIP_GITHUB=1 (print instead of setting GitHub secrets)
#   <APP>_CPU / <APP>_MEM for REDIS API FRONTEND SYSTEM DISCOVERY BROWSER (see SIZES below)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOURCE_GROUP="${RESOURCE_GROUP:-ootg-rg}"
LOCATION="${LOCATION:-eastasia}"
PREFIX="${PREFIX:-ootg}"
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-ootgadmin}"
APP_DB_USER="${APP_DB_USER:-ootg_app}"
DB_NAME="${DB_NAME:-opportunity}"
PG_SKUS="${PG_SKUS:-Standard_B1ms Standard_B2s}"
MAX_BROWSERS="${MAX_BROWSERS:-2}"
MAX_HITL_BROWSERS="${MAX_HITL_BROWSERS:-1}"
# Consumption sizes (memory = 2 x CPU). Total default: 3.25 vCPU / 6.5 GiB.
REDIS_CPU="${REDIS_CPU:-0.25}" REDIS_MEM="${REDIS_MEM:-0.5Gi}"
API_CPU="${API_CPU:-0.5}" API_MEM="${API_MEM:-1Gi}"
FRONTEND_CPU="${FRONTEND_CPU:-0.5}" FRONTEND_MEM="${FRONTEND_MEM:-1Gi}"
SYSTEM_CPU="${SYSTEM_CPU:-0.5}" SYSTEM_MEM="${SYSTEM_MEM:-1Gi}"
DISCOVERY_CPU="${DISCOVERY_CPU:-0.5}" DISCOVERY_MEM="${DISCOVERY_MEM:-1Gi}"
BROWSER_CPU="${BROWSER_CPU:-1.0}" BROWSER_MEM="${BROWSER_MEM:-2Gi}"

log() { printf '\n==> %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
# az on Windows prints CRLF: strip it, or every captured id gets a trailing \r.
azq() { az "$@" | tr -d '\r'; }
exists() { az "$@" --only-show-errors --output none >/dev/null 2>&1; }
# Paths handed to az.cmd on Windows must be native (C:\...).
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
PY=""
for candidate in python3 python; do
  if "$candidate" -c 'import sys' >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || die "python3 (or python) is required"

for var in POSTGRES_ADMIN_PASS APP_DB_PASS REDIS_PASS ENCRYPTION_KEYS JWT_SECRET; do
  [ -n "${!var:-}" ] || die "export $var first (see deploy/azure/README.md)"
done
for var in POSTGRES_ADMIN_PASS APP_DB_PASS REDIS_PASS; do
  [[ "${!var}" =~ ^[A-Za-z0-9_-]{16,}$ ]] || die "$var: use 16+ letters, digits, - or _"
done
if [ -z "${GITHUB_REPO:-}" ]; then
  GITHUB_REPO="$(git config --get remote.origin.url 2>/dev/null \
    | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##' || true)"
fi
[ -n "$GITHUB_REPO" ] || die "export GITHUB_REPO=<owner>/<repo>"

exists account show || die "run 'az login' first"
SUBSCRIPTION_ID="$(azq account show --query id -o tsv)"
TENANT_ID="$(azq account show --query tenantId -o tsv)"
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID/$RESOURCE_GROUP" | sha1sum | cut -c1-8)"
ACR_NAME="${ACR_NAME:-${PREFIX}acr${SUFFIX}}"
PG_SERVER="${PG_SERVER:-${PREFIX}-pg-${SUFFIX}}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-${PREFIX}files${SUFFIX}}"
SHARE="${PREFIX}-data"
IDENTITY="${PREFIX}-pull"
WORKSPACE="${PREFIX}-logs"
VNET="${PREFIX}-vnet"
DEPLOYER="${PREFIX}-github-deployer"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT # parameter files contain secrets

ensure_role() { # principal-object-id role scope; retried (new principals replicate slowly)
  local attempt count
  for attempt in 1 2 3 4 5 6 7 8; do
    count="$(azq role assignment list --assignee "$1" --role "$2" --scope "$3" --query 'length(@)' -o tsv 2>/dev/null || echo 0)"
    [ "$count" != "0" ] && return 0
    az role assignment create --assignee-object-id "$1" --assignee-principal-type ServicePrincipal \
      --role "$2" --scope "$3" --only-show-errors --output none 2>/dev/null && return 0
    echo "   (waiting for the principal to replicate, retry $attempt/8)"
    sleep 15
  done
  die "could not assign $2 on $3"
}

# ---- 0. providers ------------------------------------------------------------------------------
log "Resource providers + containerapp CLI extension"
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry \
  Microsoft.DBforPostgreSQL Microsoft.Storage Microsoft.Network Microsoft.ManagedIdentity; do
  az provider register --namespace "$ns" --wait --only-show-errors
done
az extension add --name containerapp --upgrade --only-show-errors

# ---- 1. resource group, registry, pull identity ------------------------------------------------
log "Resource group $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --only-show-errors --output none
RG_ID="$(azq group show --name "$RESOURCE_GROUP" --query id -o tsv)"

log "Container registry $ACR_NAME (Basic)"
exists acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  || az acr create --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --sku Basic --admin-enabled false --only-show-errors --output none
ACR_ID="$(azq acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
ACR_LOGIN_SERVER="$(azq acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"

log "Managed identity $IDENTITY (image pulls)"
exists identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" \
  || az identity create --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --only-show-errors --output none
IDENTITY_ID="$(azq identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
IDENTITY_PRINCIPAL="$(azq identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)"
ensure_role "$IDENTITY_PRINCIPAL" AcrPull "$ACR_ID"

# ---- 2. GitHub OIDC deployer + repository secrets / variables (before anything slow) ----------
log "GitHub Actions deployer $DEPLOYER for $GITHUB_REPO (main)"
APP_ID="$(azq ad app list --display-name "$DEPLOYER" --query '[0].appId' -o tsv)"
[ -n "$APP_ID" ] || APP_ID="$(azq ad app create --display-name "$DEPLOYER" --query appId -o tsv)"
exists ad sp show --id "$APP_ID" || az ad sp create --id "$APP_ID" --only-show-errors --output none
SP_OBJECT_ID="$(azq ad sp show --id "$APP_ID" --query id -o tsv)"
if [ "$(azq ad app federated-credential list --id "$APP_ID" --query "length([?name=='github-main'])" -o tsv)" = "0" ]; then
  cat >"$TMP/federated.json" <<JSON
{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:${GITHUB_REPO}:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
  az ad app federated-credential create --id "$APP_ID" \
    --parameters "@$(native "$TMP/federated.json")" --only-show-errors --output none
fi
ensure_role "$SP_OBJECT_ID" Contributor "$RG_ID" # update the container apps
ensure_role "$SP_OBJECT_ID" AcrPush "$ACR_ID"    # push images

declare -A GH_SECRETS=(
  [AZURE_CLIENT_ID]="$APP_ID"
  [AZURE_TENANT_ID]="$TENANT_ID"
  [AZURE_SUBSCRIPTION_ID]="$SUBSCRIPTION_ID"
)
declare -A GH_VARS=(
  [AZURE_RESOURCE_GROUP]="$RESOURCE_GROUP"
  [ACR_NAME]="$ACR_NAME"
  [ACR_LOGIN_SERVER]="$ACR_LOGIN_SERVER"
  [APP_PREFIX]="$PREFIX"
  [BACKEND_INTERNAL_URL]="http://${PREFIX}-api"
)
for k in "${!GH_SECRETS[@]}" "${!GH_VARS[@]}"; do
  value="${GH_SECRETS[$k]:-${GH_VARS[$k]:-}}"
  [ -n "$value" ] || die "computed an empty value for $k"
done
if [ "${SKIP_GITHUB:-0}" = 1 ]; then
  log "Set these on GitHub (Settings > Secrets and variables > Actions)"
  for k in "${!GH_SECRETS[@]}"; do echo "  secret   $k=${GH_SECRETS[$k]}"; done
  for k in "${!GH_VARS[@]}"; do echo "  variable $k=${GH_VARS[$k]}"; done
else
  command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 \
    || die "gh is not signed in: run 'gh auth login' (or SKIP_GITHUB=1 to print the values)"
  log "GitHub secrets and variables on $GITHUB_REPO"
  for k in "${!GH_SECRETS[@]}"; do
    gh secret set "$k" --repo "$GITHUB_REPO" --body "${GH_SECRETS[$k]}"
  done
  for k in "${!GH_VARS[@]}"; do
    gh variable set "$k" --repo "$GITHUB_REPO" --body "${GH_VARS[$k]}"
  done
  secrets_now="$(gh secret list --repo "$GITHUB_REPO" | tr -d '\r' | cut -f1)"
  for k in "${!GH_SECRETS[@]}"; do
    grep -qx "$k" <<<"$secrets_now" || die "GitHub secret $k is missing after setting it"
  done
  for k in "${!GH_VARS[@]}"; do
    [ "$(gh variable get "$k" --repo "$GITHUB_REPO" | tr -d '\r')" = "${GH_VARS[$k]}" ] \
      || die "GitHub variable $k does not have the expected value"
  done
  echo "   verified: ${!GH_SECRETS[*]} ${!GH_VARS[*]}"
fi

# ---- 3. logs, network, files, Container Apps environment ---------------------------------------
log "Log Analytics $WORKSPACE (0.2 GB/day cap, 30 days)"
exists monitor log-analytics workspace show --workspace-name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" \
  || az monitor log-analytics workspace create --workspace-name "$WORKSPACE" \
    --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --quota 0.2 --retention-time 30 \
    --only-show-errors --output none

log "VNet $VNET (Redis' internal TCP ingress needs a custom VNet)"
exists network vnet show --name "$VNET" --resource-group "$RESOURCE_GROUP" \
  || az network vnet create --name "$VNET" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --address-prefixes 10.42.0.0/16 --subnet-name apps --subnet-prefixes 10.42.0.0/23 \
    --only-show-errors --output none
az network vnet subnet update --vnet-name "$VNET" --resource-group "$RESOURCE_GROUP" --name apps \
  --delegations Microsoft.App/environments --only-show-errors --output none
SUBNET_ID="$(azq network vnet subnet show --vnet-name "$VNET" --resource-group "$RESOURCE_GROUP" --name apps --query id -o tsv)"

log "Storage account $STORAGE_ACCOUNT + Azure Files share $SHARE"
exists storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  || az storage account create --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
    --allow-blob-public-access false --only-show-errors --output none
exists storage share-rm show --storage-account "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --name "$SHARE" \
  || az storage share-rm create --storage-account "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --name "$SHARE" --quota 20 --only-show-errors --output none

log "Container Apps environment ${PREFIX}-env (ARM)"
az deployment group create --resource-group "$RESOURCE_GROUP" --name "${PREFIX}-environment" \
  --template-file "$(native "$SCRIPT_DIR/environment.json")" \
  --parameters location="$LOCATION" prefix="$PREFIX" subnetId="$SUBNET_ID" \
  workspaceName="$WORKSPACE" storageAccountName="$STORAGE_ACCOUNT" storageName="$SHARE" \
  --only-show-errors --output none
ENV_DOMAIN="$(azq containerapp env show --name "${PREFIX}-env" --resource-group "$RESOURCE_GROUP" --query properties.defaultDomain -o tsv)"
CONSOLE_URL="${CUSTOM_CONSOLE_URL:-https://${PREFIX}-frontend.${ENV_DOMAIN}}"

# ---- 4. PostgreSQL Flexible Server ---------------------------------------------------------------
log "PostgreSQL Flexible Server $PG_SERVER (Burstable, PostgreSQL 16)"
if ! exists postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP"; then
  created=""
  for sku in $PG_SKUS; do
    echo "   trying $sku in $LOCATION"
    # --public-access 0.0.0.0: only Azure-hosted clients (the container apps); TLS required.
    if az postgres flexible-server create --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
      --location "$LOCATION" --tier Burstable --sku-name "$sku" --version 16 --storage-size 32 \
      --admin-user "$POSTGRES_ADMIN_USER" --admin-password "$POSTGRES_ADMIN_PASS" \
      --public-access 0.0.0.0 --yes --only-show-errors --output none 2>"$TMP/pg.err"; then
      created="$sku"
      break
    fi
    grep -qiE "SkuNotAvailable|not available|capacity|restricted" "$TMP/pg.err" \
      || { cat "$TMP/pg.err" >&2; die "PostgreSQL creation failed"; }
    echo "   $sku has no capacity here; trying the next SKU"
    az postgres flexible-server delete --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
      --yes --only-show-errors >/dev/null 2>&1 || true
  done
  [ -n "$created" ] || die "no Burstable SKU in ($PG_SKUS) has capacity in $LOCATION; set LOCATION or PG_SKUS"
  echo "   created with $created"
fi
az postgres flexible-server parameter set --server-name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
  --name azure.extensions --value VECTOR --only-show-errors --output none
db_args=(--server-name "$PG_SERVER" --resource-group "$RESOURCE_GROUP")
if ! exists postgres flexible-server db show "${db_args[@]}" --name "$DB_NAME" \
  && ! exists postgres flexible-server db show "${db_args[@]}" --database-name "$DB_NAME"; then
  # The flag is --name in current CLIs and --database-name in older ones.
  az postgres flexible-server db create "${db_args[@]}" --name "$DB_NAME" --only-show-errors --output none 2>/dev/null \
    || az postgres flexible-server db create "${db_args[@]}" --database-name "$DB_NAME" --only-show-errors --output none
fi
PG_HOST="$(azq postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" --query fullyQualifiedDomainName -o tsv)"

# ---- 5. container apps (ARM template, native JSON types) ---------------------------------------
images_built() {
  local image
  for image in api worker frontend; do
    exists acr repository show --name "$ACR_NAME" --image "$PREFIX-$image:latest" || return 1
  done
}
current_image() { # app image : keep what CI deployed, else :latest
  local image
  image="$(azq containerapp show --name "$1" --resource-group "$RESOURCE_GROUP" \
    --query 'properties.template.containers[0].image' -o tsv 2>/dev/null || true)"
  case "$image" in "$ACR_LOGIN_SERVER"/*) printf '%s' "$image" ;; *) printf '%s' "$ACR_LOGIN_SERVER/$PREFIX-$2:latest" ;; esac
}
DEPLOY_APPS=false
if images_built; then DEPLOY_APPS=true; fi

log "Container apps (redis$([ "$DEPLOY_APPS" = true ] && echo ', api, workers, frontend'))"
# Secrets go through a parameters FILE (JSON-encoded by python): no quoting on any command line.
export P_LOCATION="$LOCATION" P_PREFIX="$PREFIX" P_IDENTITY="$IDENTITY_ID" \
  P_ACR="$ACR_LOGIN_SERVER" P_SHARE="$SHARE" P_DEPLOY="$DEPLOY_APPS" P_CONSOLE="$CONSOLE_URL" \
  P_MAX_BROWSERS="$MAX_BROWSERS" P_MAX_HITL="$MAX_HITL_BROWSERS" \
  P_API_IMAGE="$(current_image "$PREFIX-api" api)" \
  P_WORKER_IMAGE="$(current_image "$PREFIX-worker-browser" worker)" \
  P_FRONTEND_IMAGE="$(current_image "$PREFIX-frontend" frontend)" \
  P_DB_URL="postgresql+asyncpg://${APP_DB_USER}:${APP_DB_PASS}@${PG_HOST}:5432/${DB_NAME}?ssl=require" \
  P_DB_MIGRATION_URL="postgresql+asyncpg://${POSTGRES_ADMIN_USER}:${POSTGRES_ADMIN_PASS}@${PG_HOST}:5432/${DB_NAME}?ssl=require" \
  P_REDIS_URL="redis://:${REDIS_PASS}@${PREFIX}-redis:6379/0" P_REDIS_PASS="$REDIS_PASS" \
  P_ENCRYPTION_KEYS="$ENCRYPTION_KEYS" P_JWT_SECRET="$JWT_SECRET" \
  P_SIZES="redis=$REDIS_CPU/$REDIS_MEM api=$API_CPU/$API_MEM frontend=$FRONTEND_CPU/$FRONTEND_MEM system=$SYSTEM_CPU/$SYSTEM_MEM discovery=$DISCOVERY_CPU/$DISCOVERY_MEM browser=$BROWSER_CPU/$BROWSER_MEM"
"$PY" - "$(native "$TMP/apps.parameters.json")" <<'PYTHON'
import json, os, sys
e = os.environ
sizes = {}
for item in e["P_SIZES"].split():
    name, value = item.split("=")
    cpu, memory = value.split("/")
    sizes[name] = {"cpu": cpu, "memory": memory}
params = {
    "location": e["P_LOCATION"], "prefix": e["P_PREFIX"], "identityId": e["P_IDENTITY"],
    "acrServer": e["P_ACR"], "storageName": e["P_SHARE"], "deployApps": e["P_DEPLOY"] == "true",
    "apiImage": e["P_API_IMAGE"], "workerImage": e["P_WORKER_IMAGE"],
    "frontendImage": e["P_FRONTEND_IMAGE"], "consoleUrl": e["P_CONSOLE"],
    "maxBrowsers": e["P_MAX_BROWSERS"], "maxHitlBrowsers": e["P_MAX_HITL"], "sizes": sizes,
    "databaseUrl": e["P_DB_URL"], "databaseMigrationUrl": e["P_DB_MIGRATION_URL"],
    "redisUrl": e["P_REDIS_URL"], "redisPassword": e["P_REDIS_PASS"],
    "encryptionKeys": e["P_ENCRYPTION_KEYS"], "jwtSecret": e["P_JWT_SECRET"],
}
doc = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {k: {"value": v} for k, v in params.items()},
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PYTHON
az deployment group create --resource-group "$RESOURCE_GROUP" --name "${PREFIX}-apps" \
  --template-file "$(native "$SCRIPT_DIR/containerapps.json")" \
  --parameters "@$(native "$TMP/apps.parameters.json")" --only-show-errors --output none

# ---- done ------------------------------------------------------------------------------------------
log "Done"
if [ "$DEPLOY_APPS" = true ]; then
  FQDN="$(azq containerapp show --name "${PREFIX}-frontend" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv)"
  echo "   console: https://$FQDN"
  for attempt in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://$FQDN/api/v1/ready" || true)"
    [ "$code" = 200 ] && { echo "   https://$FQDN/api/v1/ready -> 200 (database + Redis reachable)"; break; }
    echo "   waiting for the API (migrations on first start) ... /api/v1/ready -> $code"
    sleep 15
  done
else
  echo "   images are not in $ACR_NAME yet: push to main (GitHub Actions builds them), then run this again"
fi
