#!/usr/bin/env bash
# Undo deploy/azure/pause.sh: start PostgreSQL, then one replica of every container app.
set -euo pipefail
RESOURCE_GROUP="${RESOURCE_GROUP:-ootg-rg}"
PREFIX="${PREFIX:-ootg}"
server="$(az postgres flexible-server list --resource-group "$RESOURCE_GROUP" --query '[0].name' -o tsv | tr -d '\r')"
if [ -n "$server" ]; then
  echo "==> starting PostgreSQL $server"
  az postgres flexible-server start --name "$server" --resource-group "$RESOURCE_GROUP" \
    --only-show-errors --output none || true # already running
fi
for app in redis api worker-system worker-discovery worker-browser frontend; do
  echo "==> $PREFIX-$app -> 1 replica"
  az containerapp update --name "$PREFIX-$app" --resource-group "$RESOURCE_GROUP" \
    --min-replicas 1 --max-replicas 1 --only-show-errors --output none
done
echo "resumed"
