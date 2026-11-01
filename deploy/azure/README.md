# Deploying to Azure (no Docker on your laptop)

Everything heavy happens in the cloud: **Azure Cloud Shell** runs the provisioning script (in your
browser) and **GitHub Actions** builds the Docker images and deploys them on every push to `main`.

```
GitHub push ─▶ Actions: lint/types/unit tests ─▶ build api · worker · frontend (Buildx, gha cache)
                                              ─▶ push to Azure Container Registry
                                              ─▶ az containerapp update (api first: migrations)
Azure Container Apps environment (VNet, Consumption)
  ootg-frontend   external HTTPS ─▶ proxies /api (+ WebSockets) to http://ootg-api
  ootg-api        internal; RUN_MIGRATIONS=true; remote browser sessions; /data = Azure Files
  ootg-worker-system | -discovery | -browser   arq queues (EXECUTION_MODE=arq); /data = Azure Files
  ootg-redis      internal TCP 6379, password protected
Azure Database for PostgreSQL Flexible Server 16 (Burstable B1ms, VECTOR allow-listed, TLS)
```

QLoRA training stays on Google Colab: download the dataset and notebook from
**Settings > Agent Brain & Training** in the deployed console, as before.

## The 5 steps

Open **[Azure Cloud Shell](https://shell.azure.com)** (Bash). It already has `az`, `git`, `gh`
and `python3`, and it is already signed in to Azure (otherwise run `az login`).

**1. Get the code**

```bash
git clone https://github.com/wml652/Opputunity-AI.git && cd Opputunity-AI
```

**2. Choose the secrets once** (save them in a password manager: `ENCRYPTION_KEYS` and
`JWT_SECRET` must never change, or stored data and sign-ins become unreadable)

```bash
export POSTGRES_ADMIN_PASS="$(openssl rand -base64 24)"
export APP_DB_PASS="$(openssl rand -base64 24)"
export REDIS_PASS="$(openssl rand -hex 24)"
export ENCRYPTION_KEYS="1:$(openssl rand -base64 32)"
export JWT_SECRET="$(openssl rand -hex 32)"
export LOCATION=eastus          # optional; any region with Container Apps + Flexible Server
echo "$POSTGRES_ADMIN_PASS $APP_DB_PASS $REDIS_PASS $ENCRYPTION_KEYS $JWT_SECRET"  # save these
```

**3. Let the script set GitHub up for you, then provision** (first run: 10-20 minutes;
infrastructure plus the GitHub OIDC login, secrets and variables)

```bash
gh auth login        # once; lets the script store the GitHub secrets / variables itself
bash deploy/azure/provision-azure.sh
```

**4. Build the images in the cloud** (push anything to `main`, or start the workflow by hand)

```bash
gh workflow run deploy-azure.yml --repo wml652/Opputunity-AI && gh run watch --repo wml652/Opputunity-AI
```

**5. Create the apps from those images** (same command, second run; it only adds what is missing)

```bash
bash deploy/azure/provision-azure.sh
```

The script prints the console URL (`https://ootg-frontend.<random>.<region>.azurecontainerapps.io`).
From now on **every `git push` to `main` builds and deploys automatically**: no step on your
laptop besides `git push`.

## What the script does (idempotent: re-run it any time)

| Resource | Notes |
|---|---|
| Resource group, Container Registry (Basic) | Images are pushed by GitHub Actions only |
| Managed identity `ootg-pull` | `AcrPull`; the apps pull images without registry passwords |
| PostgreSQL Flexible Server 16, `Standard_B1ms` | `azure.extensions=VECTOR`; database `opportunity`; TLS (`?ssl=require`); reachable from Azure services only |
| App database role | Created by `migrate` on the API's first start: `LOGIN NOSUPERUSER NOBYPASSRLS` so row-level security applies |
| VNet + delegated subnet | Container Apps needs a custom VNet for Redis' internal TCP ingress |
| Container Apps environment + Log Analytics | Workload-profiles environment, Consumption profile |
| Azure Files share `ootg-data` at `/data` | Encrypted CV / transcript blobs written by `api`, read by `worker-browser`; mounted as uid 10001 |
| Apps `ootg-redis`, `ootg-api`, `ootg-worker-*`, `ootg-frontend` | Sizes, secrets, probes and volumes in generated YAML; re-runs keep the image CI deployed |
| Entra app `ootg-github-deployer` | GitHub OIDC federated credential for `main` (no stored passwords); `Contributor` on the resource group, `AcrPush` on the registry |

GitHub configuration it sets (or prints, if `gh` is not signed in):

- secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- variables `AZURE_RESOURCE_GROUP`, `ACR_NAME`, `ACR_LOGIN_SERVER`, `APP_PREFIX`, `BACKEND_INTERNAL_URL` (`http://ootg-api`)

## Costs and sizing

Everything runs 24/7 with one replica each (4.75 vCPU / 9.5 GiB in total by default), so the
bill is dominated by Container Apps compute. Roughly: PostgreSQL B1ms about 15 USD/month,
Registry Basic about 5 USD/month, Azure Files a few cents, Container Apps the rest - check the
[pricing calculator](https://azure.microsoft.com/pricing/calculator/) for your region (student
credits cover a lot of it). To shrink it, export smaller sizes before running the script, for
example `export BROWSER_CPU=1.0 BROWSER_MEM=2Gi MAX_BROWSERS=2 API_CPU=1.0 API_MEM=2Gi
MAX_HITL_BROWSERS=1` (Consumption needs memory = 2 x CPU, in 0.25 vCPU steps).

## Notes

- **Redis** runs as a container without persistence: if it restarts, jobs still waiting in the
  queue are lost (running campaigns and fills are resumed from the database by the resume sweep).
  For persistence, switch `REDIS_URL` to a managed Azure Redis (`rediss://...`).
- **Custom domain**: add it to `ootg-frontend`, then re-run the script with
  `CUSTOM_CONSOLE_URL=https://your.domain` so cookies, CORS and the WebSocket Origin check match.
- **Logs**: `az containerapp logs show -n ootg-api -g ootg-rg --follow` (or Log Analytics).
- **Local copy of the same topology**: `docker compose --env-file deploy/compose/compose.env up`
  (see `docker-compose.yml`) - only on a machine with enough memory.
