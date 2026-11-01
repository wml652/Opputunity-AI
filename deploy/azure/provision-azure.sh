#!/usr/bin/env bash
# Provision OPPORTUNITIES ON THE GO on Azure (idempotent: safe to run again; existing resources
# are reused and the images CI deployed are kept). Run it in Azure Cloud Shell (Bash) from a
# clone of the repository - nothing is built locally; GitHub Actions builds the images.
#
# Two passes on a fresh subscription (the apps need images, the images need the registry):
#   1st run  infrastructure + GitHub OIDC/secrets/variables (apps skipped: no images yet)
#   push     GitHub Actions builds api / worker / frontend images into the registry
#   2nd run  creates the container apps from those images; every later push deploys itself
#
#   Resource group
#   ├─ Container Registry (Basic)                      images pushed by GitHub Actions
#   ├─ Managed identity  <prefix>-pull                 AcrPull for the container apps
#   ├─ Log Analytics workspace                         container logs
#   ├─ VNet + subnet (delegated to Microsoft.App)      needed for Redis' internal TCP ingress
#   ├─ Container Apps environment (workload profiles, Consumption)
#   │    ├─ storage "<prefix>-data" = Azure Files share, mounted at /data in every backend app
#   │    ├─ <prefix>-redis            redis:7, internal TCP ingress, password protected
#   │    ├─ <prefix>-api              internal HTTP ingress, RUN_MIGRATIONS=true, remote browsers
#   │    ├─ <prefix>-worker-system    outbox relay, deadline + resume sweeps
#   │    ├─ <prefix>-worker-discovery campaigns (crawler Chromium)
#   │    ├─ <prefix>-worker-browser   fills / submissions (Chromium pool)
#   │    └─ <prefix>-frontend         external HTTPS ingress; proxies /api (+ WebSockets) to api
#   ├─ PostgreSQL Flexible Server 16, Burstable Standard_B1ms, VECTOR allow-listed
#   └─ Storage account (Azure Files share)
#   + Entra app "<prefix>-github-deployer" with a GitHub OIDC federated credential (no secrets)
#
# Required (export before running; keep them in a password manager - ENCRYPTION_KEYS and
# JWT_SECRET must stay the same forever, or stored data / sessions become unreadable):
#   POSTGRES_ADMIN_PASS  APP_DB_PASS  REDIS_PASS  ENCRYPTION_KEYS  JWT_SECRET
# Optional (defaults in brackets):
#   RESOURCE_GROUP [ootg-rg]  LOCATION [eastus]  PREFIX [ootg]  GITHUB_REPO [from git remote]
#   POSTGRES_ADMIN_USER [ootgadmin]  APP_DB_USER [ootg_app]  DB_NAME [opportunity]
#   MAX_BROWSERS [3]  MAX_HITL_BROWSERS [2]  CUSTOM_CONSOLE_URL [https://<frontend fqdn>]
#   API_CPU/API_MEM [1.5/3Gi]  BROWSER_CPU/BROWSER_MEM [1.5/3Gi]
#   DISCOVERY_CPU/DISCOVERY_MEM [0.75/1.5Gi]  SYSTEM_CPU/SYSTEM_MEM [0.25/0.5Gi]
#   FRONTEND_CPU/FRONTEND_MEM [0.5/1Gi]  REDIS_CPU/REDIS_MEM [0.25/0.5Gi]
set -euo pipefail

# ---- parameters ------------------------------------------------------------------------------
RESOURCE_GROUP="${RESOURCE_GROUP:-ootg-rg}"
LOCATION="${LOCATION:-eastus}"
PREFIX="${PREFIX:-ootg}"
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-ootgadmin}"
APP_DB_USER="${APP_DB_USER:-ootg_app}"
DB_NAME="${DB_NAME:-opportunity}"
MAX_BROWSERS="${MAX_BROWSERS:-3}"
MAX_HITL_BROWSERS="${MAX_HITL_BROWSERS:-2}"
API_CPU="${API_CPU:-1.5}" API_MEM="${API_MEM:-3Gi}"
BROWSER_CPU="${BROWSER_CPU:-1.5}" BROWSER_MEM="${BROWSER_MEM:-3Gi}"
DISCOVERY_CPU="${DISCOVERY_CPU:-0.75}" DISCOVERY_MEM="${DISCOVERY_MEM:-1.5Gi}"
SYSTEM_CPU="${SYSTEM_CPU:-0.25}" SYSTEM_MEM="${SYSTEM_MEM:-0.5Gi}"
FRONTEND_CPU="${FRONTEND_CPU:-0.5}" FRONTEND_MEM="${FRONTEND_MEM:-1Gi}"
REDIS_CPU="${REDIS_CPU:-0.25}" REDIS_MEM="${REDIS_MEM:-0.5Gi}"

for var in POSTGRES_ADMIN_PASS APP_DB_PASS REDIS_PASS ENCRYPTION_KEYS JWT_SECRET; do
  if [ -z "${!var:-}" ]; then
    echo "error: export $var first (see deploy/azure/README.md)" >&2
    exit 1
  fi
done
if [ -z "${GITHUB_REPO:-}" ]; then
  remote="$(git config --get remote.origin.url 2>/dev/null || true)"
  GITHUB_REPO="$(printf '%s' "$remote" | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"
fi
if [ -z "$GITHUB_REPO" ]; then
  echo "error: export GITHUB_REPO=<owner>/<repo>" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }
exists() { "$@" --only-show-errors --output none >/dev/null 2>&1; }

az account show --output none || { echo "error: run 'az login' first" >&2; exit 1; }
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
# Globally unique names, stable for this subscription + resource group.
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID/$RESOURCE_GROUP" | sha1sum | cut -c1-8)"
ACR_NAME="${ACR_NAME:-${PREFIX}acr${SUFFIX}}"
PG_SERVER="${PG_SERVER:-${PREFIX}-pg-${SUFFIX}}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-${PREFIX}files${SUFFIX}}"
ENV_NAME="${PREFIX}-env"
VNET="${PREFIX}-vnet"
SHARE="${PREFIX}-data"
IDENTITY="${PREFIX}-pull"
WORKSPACE="${PREFIX}-logs"

urlenc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# YAML single-quoted scalar (secrets may contain any character).
yq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }

# ---- providers & CLI extension ----------------------------------------------------------------
log "Registering resource providers (first run only takes a few minutes)"
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry \
  Microsoft.DBforPostgreSQL Microsoft.Storage Microsoft.Network Microsoft.ManagedIdentity; do
  az provider register --namespace "$ns" --wait --only-show-errors
done
az extension add --name containerapp --upgrade --only-show-errors

# ---- resource group, registry, pull identity ---------------------------------------------------
log "Resource group $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

log "Container registry $ACR_NAME"
exists az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  || az acr create --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --sku Basic --admin-enabled false --output none
ACR_ID="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"

ensure_role() { # principal-object-id role scope (retried: new identities take a while to replicate)
  local count attempt
  for attempt in 1 2 3 4 5 6; do
    count="$(az role assignment list --assignee "$1" --role "$2" --scope "$3" --query 'length(@)' -o tsv 2>/dev/null || echo 0)"
    [ "$count" != "0" ] && return 0
    if az role assignment create --assignee-object-id "$1" --assignee-principal-type ServicePrincipal \
      --role "$2" --scope "$3" --output none --only-show-errors; then
      return 0
    fi
    echo "   (role assignment not possible yet, retry $attempt/6 in 15 s)"
    sleep 15
  done
  echo "error: could not assign $2 on $3" >&2
  return 1
}

log "Managed identity $IDENTITY (pulls images from the registry)"
exists az identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" \
  || az identity create --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --output none
IDENTITY_ID="$(az identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
IDENTITY_PRINCIPAL="$(az identity show --name "$IDENTITY" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)"
ensure_role "$IDENTITY_PRINCIPAL" AcrPull "$ACR_ID"

# ---- logs, network, Container Apps environment -------------------------------------------------
log "Log Analytics workspace $WORKSPACE"
exists az monitor log-analytics workspace show --workspace-name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" \
  || az monitor log-analytics workspace create --workspace-name "$WORKSPACE" \
    --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --output none
WS_ID="$(az monitor log-analytics workspace show --workspace-name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" --query customerId -o tsv)"
WS_KEY="$(az monitor log-analytics workspace get-shared-keys --workspace-name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" --query primarySharedKey -o tsv)"

log "Virtual network $VNET (internal TCP ingress for Redis needs a custom VNet)"
exists az network vnet show --name "$VNET" --resource-group "$RESOURCE_GROUP" \
  || az network vnet create --name "$VNET" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --address-prefixes 10.42.0.0/16 --subnet-name apps --subnet-prefixes 10.42.0.0/23 --output none
az network vnet subnet update --vnet-name "$VNET" --resource-group "$RESOURCE_GROUP" --name apps \
  --delegations Microsoft.App/environments --output none
SUBNET_ID="$(az network vnet subnet show --vnet-name "$VNET" --resource-group "$RESOURCE_GROUP" --name apps --query id -o tsv)"

log "Container Apps environment $ENV_NAME"
exists az containerapp env show --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" \
  || az containerapp env create --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --enable-workload-profiles \
    --infrastructure-subnet-resource-id "$SUBNET_ID" \
    --logs-destination log-analytics --logs-workspace-id "$WS_ID" --logs-workspace-key "$WS_KEY" \
    --output none
ENV_ID="$(az containerapp env show --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
ENV_DOMAIN="$(az containerapp env show --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" --query properties.defaultDomain -o tsv)"
CONSOLE_URL="${CUSTOM_CONSOLE_URL:-https://${PREFIX}-frontend.${ENV_DOMAIN}}"

# ---- Azure Files share at /data -----------------------------------------------------------------
log "Azure Files share $SHARE (encrypted documents shared by api and workers)"
exists az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  || az storage account create --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
    --allow-blob-public-access false --output none
STORAGE_KEY="$(az storage account keys list --account-name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query '[0].value' -o tsv)"
exists az storage share-rm show --storage-account "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --name "$SHARE" \
  || az storage share-rm create --storage-account "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --name "$SHARE" --quota 50 --output none
az containerapp env storage set --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" \
  --storage-name "$SHARE" --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "$STORAGE_KEY" --azure-file-share-name "$SHARE" \
  --access-mode ReadWrite --output none

# ---- PostgreSQL Flexible Server ------------------------------------------------------------------
log "PostgreSQL Flexible Server $PG_SERVER (Burstable Standard_B1ms, PostgreSQL 16)"
if ! exists az postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP"; then
  # --public-access 0.0.0.0 = only Azure-hosted clients (the container apps); TLS is required.
  az postgres flexible-server create --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --tier Burstable --sku-name Standard_B1ms --version 16 \
    --storage-size 32 --admin-user "$POSTGRES_ADMIN_USER" --admin-password "$POSTGRES_ADMIN_PASS" \
    --public-access 0.0.0.0 --yes --output none
fi
az postgres flexible-server parameter set --server-name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
  --name azure.extensions --value VECTOR --output none
exists az postgres flexible-server db show --server-name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" --database-name "$DB_NAME" \
  || az postgres flexible-server db create --server-name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" \
    --database-name "$DB_NAME" --output none
PG_HOST="$(az postgres flexible-server show --name "$PG_SERVER" --resource-group "$RESOURCE_GROUP" --query fullyQualifiedDomainName -o tsv)"
# The app connects as a NON-superuser role (RLS applies); `migrate` creates it on first start.
DATABASE_URL="postgresql+asyncpg://${APP_DB_USER}:$(urlenc "$APP_DB_PASS")@${PG_HOST}:5432/${DB_NAME}?ssl=require"
DATABASE_MIGRATION_URL="postgresql+asyncpg://${POSTGRES_ADMIN_USER}:$(urlenc "$POSTGRES_ADMIN_PASS")@${PG_HOST}:5432/${DB_NAME}?ssl=require"
REDIS_URL="redis://:$(urlenc "$REDIS_PASS")@${PREFIX}-redis:6379/0"

# ---- container apps (YAML: volumes, probes and secrets are not all exposed as CLI flags) -------
WORKDIR_TMP="$(mktemp -d)"
trap 'rm -rf "$WORKDIR_TMP"' EXIT # the YAML files contain secrets

current_image() { # app image-name : keep what CI deployed, else the latest build
  local image
  image="$(az containerapp show --name "$1" --resource-group "$RESOURCE_GROUP" \
    --query 'properties.template.containers[0].image' -o tsv 2>/dev/null || true)"
  printf '%s' "${image:-$ACR_LOGIN_SERVER/$PREFIX-$2:latest}"
}

images_built() {
  local image
  for image in api worker frontend; do
    exists az acr repository show --name "$ACR_NAME" --image "$PREFIX-$image:latest" || return 1
  done
}

deploy_app() { # name yaml-file
  if exists az containerapp show --name "$1" --resource-group "$RESOURCE_GROUP"; then
    az containerapp update --name "$1" --resource-group "$RESOURCE_GROUP" --yaml "$2" --output none
  else
    az containerapp create --name "$1" --resource-group "$RESOURCE_GROUP" --yaml "$2" --output none
  fi
}

backend_secrets() {
  cat <<YAML
    secrets:
      - name: database-url
        value: $(yq "$DATABASE_URL")
      - name: database-migration-url
        value: $(yq "$DATABASE_MIGRATION_URL")
      - name: redis-url
        value: $(yq "$REDIS_URL")
      - name: encryption-keys
        value: $(yq "$ENCRYPTION_KEYS")
      - name: jwt-secret
        value: $(yq "$JWT_SECRET")
YAML
}

backend_env() {
  cat <<YAML
          - name: EXECUTION_MODE
            value: arq
          - name: HITL_MODE
            value: remote
          - name: DATABASE_URL
            secretRef: database-url
          - name: DATABASE_MIGRATION_URL
            secretRef: database-migration-url
          - name: REDIS_URL
            secretRef: redis-url
          - name: ENCRYPTION_KEYS
            secretRef: encryption-keys
          - name: JWT_SECRET
            secretRef: jwt-secret
          - name: COOKIE_SECURE
            value: "true"
          - name: CONSOLE_URL
            value: $(yq "$CONSOLE_URL")
          - name: CORS_ORIGINS
            value: $(yq "[\"$CONSOLE_URL\"]")
          - name: OUTBOX_RELAY_IN_API
            value: "false"
          - name: MAX_BROWSERS
            value: "$MAX_BROWSERS"
          - name: MAX_HITL_BROWSERS
            value: "$MAX_HITL_BROWSERS"
YAML
}

app_header() { # name
  cat <<YAML
name: $1
location: $LOCATION
identity:
  type: UserAssigned
  userAssignedIdentities:
    "$IDENTITY_ID": {}
properties:
  managedEnvironmentId: $ENV_ID
  workloadProfileName: Consumption
YAML
}

data_volume() {
  cat <<YAML
    volumes:
      - name: data
        storageType: AzureFile
        storageName: $SHARE
        mountOptions: "uid=10001,gid=10001,dir_mode=0770,file_mode=0660,nobrl"
YAML
}

registries() {
  cat <<YAML
    registries:
      - server: $ACR_LOGIN_SERVER
        identity: $IDENTITY_ID
YAML
}

log "Container app ${PREFIX}-redis (internal TCP, password protected)"
cat >"$WORKDIR_TMP/redis.yaml" <<YAML
$(app_header "${PREFIX}-redis")
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: false
      transport: tcp
      targetPort: 6379
      exposedPort: 6379
    secrets:
      - name: redis-pass
        value: $(yq "$REDIS_PASS")
  template:
    containers:
      - name: redis
        image: redis:7-alpine
        command: ["sh", "-c"]
        # Queues and the browser pool live here: never evict keys under memory pressure.
        args: ['exec redis-server --requirepass "\$REDIS_PASSWORD" --maxmemory 256mb --maxmemory-policy noeviction']
        env:
          - name: REDIS_PASSWORD
            secretRef: redis-pass
        resources: {cpu: $REDIS_CPU, memory: $REDIS_MEM}
    scale: {minReplicas: 1, maxReplicas: 1}
YAML
deploy_app "${PREFIX}-redis" "$WORKDIR_TMP/redis.yaml"

deploy_apps() {
log "Container app ${PREFIX}-api (internal HTTP ingress; migrations on start; remote browsers)"
cat >"$WORKDIR_TMP/api.yaml" <<YAML
$(app_header "${PREFIX}-api")
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: false
      transport: auto        # HTTP/1.1 + WebSocket upgrades (remote browser stream)
      targetPort: 8000
      allowInsecure: true    # the frontend calls http://${PREFIX}-api inside the environment
$(registries)
$(backend_secrets)
  template:
    containers:
      - name: api
        image: $(current_image "${PREFIX}-api" api)
        env:
$(backend_env)
          - name: RUN_MIGRATIONS
            value: "true"
        resources: {cpu: $API_CPU, memory: $API_MEM}
        volumeMounts:
          - volumeName: data
            mountPath: /data
        probes:
          - type: Startup          # migrations run before the server listens
            httpGet: {path: /api/v1/health, port: 8000}
            periodSeconds: 10
            failureThreshold: 30
          - type: Liveness
            httpGet: {path: /api/v1/health, port: 8000}
            periodSeconds: 30
          - type: Readiness
            httpGet: {path: /api/v1/ready, port: 8000}
            periodSeconds: 15
    # One replica: remote human sessions live in this process (frames relay through Redis, so
    # more replicas work, but one is enough and keeps the bill down).
    scale: {minReplicas: 1, maxReplicas: 1}
$(data_volume)
YAML
deploy_app "${PREFIX}-api" "$WORKDIR_TMP/api.yaml"

worker_app() { # queue cpu memory
  local name="${PREFIX}-worker-$1"
  log "Container app $name"
  cat >"$WORKDIR_TMP/$name.yaml" <<YAML
$(app_header "$name")
  configuration:
    activeRevisionsMode: Single
$(registries)
$(backend_secrets)
  template:
    containers:
      - name: worker
        image: $(current_image "$name" worker)
        env:
$(backend_env)
          - name: WORKER_QUEUE
            value: $1
        resources: {cpu: $2, memory: $3}
        volumeMounts:
          - volumeName: data
            mountPath: /data
    scale: {minReplicas: 1, maxReplicas: 1}
$(data_volume)
YAML
  deploy_app "$name" "$WORKDIR_TMP/$name.yaml"
}
worker_app system "$SYSTEM_CPU" "$SYSTEM_MEM"
worker_app discovery "$DISCOVERY_CPU" "$DISCOVERY_MEM"
worker_app browser "$BROWSER_CPU" "$BROWSER_MEM"

log "Container app ${PREFIX}-frontend (external HTTPS ingress)"
cat >"$WORKDIR_TMP/frontend.yaml" <<YAML
$(app_header "${PREFIX}-frontend")
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      transport: auto        # WebSocket upgrades are proxied to the api (/api/v1/hitl/...)
      targetPort: 3000
      allowInsecure: false
$(registries)
  template:
    containers:
      - name: frontend
        image: $(current_image "${PREFIX}-frontend" frontend)
        resources: {cpu: $FRONTEND_CPU, memory: $FRONTEND_MEM}
    scale: {minReplicas: 1, maxReplicas: 2}
YAML
deploy_app "${PREFIX}-frontend" "$WORKDIR_TMP/frontend.yaml"
}

APPS_DEPLOYED=false
if images_built; then
  deploy_apps
  APPS_DEPLOYED=true
else
  log "Images not in $ACR_NAME yet: skipping the api / worker / frontend apps for now"
  echo "   Push to main (or run the workflow) so GitHub Actions builds them, then run this again."
fi

# ---- GitHub Actions deployer (OIDC federated credential: no stored passwords) -------------------
log "GitHub Actions deployer for $GITHUB_REPO (main branch)"
DEPLOYER="${PREFIX}-github-deployer"
APP_ID="$(az ad app list --display-name "$DEPLOYER" --query '[0].appId' -o tsv)"
if [ -z "$APP_ID" ]; then
  APP_ID="$(az ad app create --display-name "$DEPLOYER" --query appId -o tsv)"
fi
exists az ad sp show --id "$APP_ID" || az ad sp create --id "$APP_ID" --output none
SP_OBJECT_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"
if [ "$(az ad app federated-credential list --id "$APP_ID" --query "length([?name=='github-main'])" -o tsv)" = "0" ]; then
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"github-main\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"repo:${GITHUB_REPO}:ref:refs/heads/main\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" --output none
fi
RG_ID="$(az group show --name "$RESOURCE_GROUP" --query id -o tsv)"
ensure_role "$SP_OBJECT_ID" Contributor "$RG_ID" # update container apps
ensure_role "$SP_OBJECT_ID" AcrPush "$ACR_ID"    # push images

# ---- GitHub secrets & variables ------------------------------------------------------------------
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
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  log "Setting GitHub secrets and variables on $GITHUB_REPO"
  for k in "${!GH_SECRETS[@]}"; do gh secret set "$k" --repo "$GITHUB_REPO" --body "${GH_SECRETS[$k]}"; done
  for k in "${!GH_VARS[@]}"; do gh variable set "$k" --repo "$GITHUB_REPO" --body "${GH_VARS[$k]}"; done
else
  log "Set these on GitHub (Settings > Secrets and variables > Actions), or run 'gh auth login' and re-run"
  for k in "${!GH_SECRETS[@]}"; do echo "  secret   $k=${GH_SECRETS[$k]}"; done
  for k in "${!GH_VARS[@]}"; do echo "  variable $k=${GH_VARS[$k]}"; done
fi

log "Done"
if [ "$APPS_DEPLOYED" = true ]; then
  echo "  console: $CONSOLE_URL"
  echo "  every push to main now builds and deploys automatically"
else
  echo "  next: push to main (GitHub builds the images), then run this script again"
fi
