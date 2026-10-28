# OPPORTUNITIES ON THE GO

Autonomous opportunity discovery, stylometric essay synthesis, self-learning Q&A memory, and a
human-in-the-loop auto-application console. Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Project rules: [`CLAUDE.md`](CLAUDE.md).

| Path | Stack |
|---|---|
| `backend/` | Python 3.12 · FastAPI · SQLAlchemy 2 (asyncpg) · Alembic · arq · PostgreSQL 16 + pgvector · Redis |
| `frontend/` | Next.js 15 (App Router) · TypeScript strict · Tailwind v4 · Radix UI · TanStack Query · zod |

## One-click launch (Windows)

Double-click **Opportunities On The Go** on the Desktop (or `Launch Mission Control.cmd` in the repo root).
It configures PATH for `uv`/`pnpm`, starts the embedded PostgreSQL + migrations, the API (:8000)
and console (:3000) as background processes, waits until both answer, and opens
http://localhost:3000. Double-clicking again while it runs just reopens the browser.

| Action | How |
|---|---|
| Start / reopen | Desktop icon, `Launch Mission Control.cmd`, or `python scripts/launcher.py` |
| Start with no console window | `Launch Mission Control (silent).vbs` |
| Stop | `Stop Mission Control` shortcut, or `python scripts/launcher.py --stop` |
| Status | `python scripts/launcher.py --status` |
| Recreate shortcuts/icon | `python scripts/launcher.py --install` |
| Logs | `.logs/` (`api.log`, `web.log`, `launcher.log`, `bootstrap.log`) |

The launcher enables `DEV_AUTH_BYPASS=true` (local dev only), so you land on the console without
logging in. `next dev` uses `.next-dev/` and `next build` uses `.next/`, so builds never break a
running dev server; a stale dev cache is detected and cleared automatically.

### Discovery sources

Web search works with zero keys (DuckDuckGo via `ddgs`). For better results, add `TAVILY_API_KEY`
and/or `BRAVE_API_KEY` to `backend/.env` and restart. Headless Chromium is installed with
`uv run playwright install chromium`; without it the crawler falls back to plain HTTP.

### Notifications (email / WhatsApp)

The filler alerts you when it pauses on a question, is blocked, or is ready for your OK. Messages
contain no personal details and no one-tap submit, only a link back to the console. Add channels
under **System → Notifications** (each one is verified with a 6-digit code). Configure providers
in `backend/.env`:

| Channel | Variables |
|---|---|
| Email (Resend) | `RESEND_API_KEY`, `RESEND_SENDER` |
| Email (SMTP) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_SENDER`, `SMTP_STARTTLS` |
| WhatsApp (Twilio) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` |
| WhatsApp (Meta Cloud API) | `META_WA_ACCESS_TOKEN`, `META_WA_PHONE_NUMBER_ID`, `META_WA_TEMPLATE`, `META_APP_SECRET`, `META_VERIFY_TOKEN` |
| Delivery-receipt webhooks | `PUBLIC_API_URL`, then point Twilio at `/api/v1/webhooks/twilio` and Meta at `/api/v1/webhooks/meta` (signatures verified) |

With no provider configured in development, messages (including verification codes) are written to
`.logs/notifications.log` instead of being delivered.

### Flight recorder

Every crawled page records a step-by-step trace (discovery → preflight/SSRF → fetch → distill →
LLM extraction). Click a page row on **Campaigns** to open the Crawl Inspector, or read
`.logs/campaigns/campaign_<id>.jsonl` / `.log`. Failures are remembered per domain
(`domain_playbooks`), so the next campaign adapts automatically.

## Native local setup (no Docker)

Prerequisites: [`uv`](https://docs.astral.sh/uv/), Node 20+ with `pnpm`, a PostgreSQL 16 database
with pgvector (Neon/Supabase free tier, or local), and Redis (Upstash, or Memurai on Windows).

```bash
# 1. Backend environment
cp .env.example backend/.env
cd backend
uv sync
uv run python -m app.infrastructure.crypto.keygen 1      # paste into ENCRYPTION_KEYS in backend/.env
# set DATABASE_URL (non-superuser role!) and REDIS_URL in backend/.env

# 2. Database schema
uv run alembic upgrade head

# 3. Run (three terminals)
uv run serve                      # API on http://127.0.0.1:8000  (docs: /api/docs)
uv run worker --queue system      # outbox relay + worker heartbeat
cd ../frontend && pnpm install && pnpm dev    # console on http://localhost:3000
```

On Windows, always use `uv run serve` / `uv run worker` (not raw `uvicorn --reload` or the `arq`
CLI): they pin the Proactor event loop that Playwright needs (see `CLAUDE.md` §3).

### Creating a non-superuser app role (local Postgres)

```sql
CREATE ROLE app LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE opportunity OWNER app;
\c opportunity
CREATE EXTENSION IF NOT EXISTS vector;   -- needs a superuser once
```

## Verification gate

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run lint-imports
uv run pytest -q --cov            # embedded Postgres via pgserver; or set TEST_DATABASE_URL
cd ..
pnpm -C frontend typecheck && pnpm -C frontend lint && pnpm -C frontend build
```
