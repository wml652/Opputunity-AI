#!/usr/bin/env bash
# Stop paying for compute while you are not using the app: every container app goes to 0
# replicas and PostgreSQL is stopped. Data (database, Azure Files, registry) is kept.
# Note: Azure starts a stopped Flexible Server again automatically after 7 days.
# Undo with deploy/azure/resume.sh.
set -euo pipefail
RESOURCE_GROUP="${RESOURCE_GROUP:-ootg-rg}"
PREFIX="${PREFIX:-ootg}"
for app in frontend api worker-system worker-discovery worker-browser redis; do
  echo "==> $PREFIX-$app -> 0 replicas"
  az containerapp update --name "$PREFIX-$app" --resource-group "$RESOURCE_GROUP" \
    --min-replicas 0 --max-replicas 1 --only-show-errors --output none
done
server="$(az postgres flexible-server list --resource-group "$RESOURCE_GROUP" --query '[0].name' -o tsv | tr -d '\r')"
if [ -n "$server" ]; then
  echo "==> stopping PostgreSQL $server"
  az postgres flexible-server stop --name "$server" --resource-group "$RESOURCE_GROUP" --only-show-errors --output none
fi
echo "paused (the console wakes on the next visit but needs resume.sh for the database and workers)"
