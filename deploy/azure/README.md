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

Run it in **[Azure Cloud Shell](https://shell.azure.com)** (Bash) or in **Git Bash on Windows**
with the Azure CLI and GitHub CLI installed (`az login`, `gh auth login`). Nothing is built
locally either way.

The script runs in this order, so CI is never left without credentials:
1. resource group, registry and pull identity;
2. GitHub OIDC deployer and the repository secrets and variables (checked with `gh` right away);
3. Log Analytics, VNet, Azure Files share and the Container Apps environment;
4. PostgreSQL;
5. the apps.

Both the environment and the apps are deployed from ARM templates (`environment.json`,
`containerapps.json`) - native JSON types, no CLI YAML conversion.

**1. Get the code**

```bash
git clone https://github.com/wml652/Opputunity-AI.git && cd Opputunity-AI
```

**2. Choose the secrets once** (save them in a password manager: `ENCRYPTION_KEYS` and
`JWT_SECRET` must never change, or stored data and sign-ins become unreadable)

```bash
export POSTGRES_ADMIN_PASS="$(openssl rand -hex 20)"   # letters/digits only: they pass through az's command line
export APP_DB_PASS="$(openssl rand -hex 20)"
export REDIS_PASS="$(openssl rand -hex 24)"
export ENCRYPTION_KEYS="1:$(openssl rand -base64 32)"
export JWT_SECRET="$(openssl rand -hex 32)"
export LOCATION=eastasia        # Azure for Students allows: centralindia polandcentral eastasia uaenorth austriaeast
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

Defaults (one replica each, always on): redis 0.25 vCPU / 0.5 GiB; api, frontend, system and
discovery workers 0.5 / 1 GiB each; browser worker 1.0 / 2 GiB - 3.25 vCPU / 6.5 GiB in total,
with `MAX_BROWSERS=2` and `MAX_HITL_BROWSERS=1`. PostgreSQL B1ms (B2s only if B1ms has no
capacity), Registry Basic, Log Analytics capped at 0.2 GB/day.

Container Apps bills per second of allocated vCPU / GiB. A replica that is busy all month at
these sizes costs far more than a 100 USD student credit lasts; idle replicas are billed at a much
lower rate, but workers that poll Redis may not count as idle. Watch **Cost Management** in the
portal during the first days, and pause everything when you are not using it:

```bash
bash deploy/azure/pause.sh      # all apps to 0 replicas + stop PostgreSQL (data is kept)
bash deploy/azure/resume.sh     # start PostgreSQL + 1 replica each
```

Override sizes before running the script (memory = 2 x CPU, 0.25 vCPU steps), e.g.
`export SYSTEM_CPU=0.25 SYSTEM_MEM=0.5Gi FRONTEND_CPU=0.25 FRONTEND_MEM=0.5Gi`.

## Notes

- **Redis** runs as a container without persistence: if it restarts, jobs still waiting in the
  queue are lost (running campaigns and fills are resumed from the database by the resume sweep).
  For persistence, switch `REDIS_URL` to a managed Azure Redis (`rediss://...`).
- **Custom domain**: add it to `ootg-frontend`, then re-run the script with
  `CUSTOM_CONSOLE_URL=https://your.domain` so cookies, CORS and the WebSocket Origin check match.
- **Logs**: `az containerapp logs show -n ootg-api -g ootg-rg --follow` (or Log Analytics).
- **Local copy of the same topology**: `docker compose --env-file deploy/compose/compose.env up`
  (see `docker-compose.yml`) - only on a machine with enough memory.
