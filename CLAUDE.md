# Project Constitution

Autonomous Opportunity Discovery, Stylometric Essay Synthesis, Self-Learning Q&A Memory, and
Human-in-the-Loop Auto-Application platform. Full blueprint: `docs/ARCHITECTURE.md`.

Monorepo: `backend/` (Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, arq, Playwright)
and `frontend/` (Next.js 15 App Router, TypeScript strict, Tailwind v4, Radix UI, TanStack Query, zod).
Local development is native (`uv` + `pnpm`, no Docker). Dockerfiles exist only for cloud hosting.

## 1. Hexagonal Architecture Laws (enforced by `import-linter`, see `backend/pyproject.toml`)

Layers, top to bottom. A layer may import only layers below it.

| Layer | Package | May import |
|---|---|---|
| Composition root | `app.bootstrap`, `app.main_worker`, `app.serve` | everything |
| Adapters (independent siblings) | `app.api`, `app.infrastructure` | `app.application`, `app.domain` — never each other |
| Use cases | `app.application` | `app.domain` only |
| Core | `app.domain` | nothing internal; stdlib only |

- `app.domain` and `app.application` must not import frameworks (FastAPI, Starlette, SQLAlchemy,
  Pydantic, httpx, openai, redis, arq, cryptography, structlog, jwt, argon2, playwright).
  They use `dataclasses`, `enum`, `typing.Protocol`.
- Ports (Protocols) live in `app.domain.ports`. Adapters implement them in `app.infrastructure`.
- Wiring happens ONLY in `app.bootstrap` (API) and `app.main_worker` (workers). Routers receive
  ports through `app.api.container.ApiContainer`, never concrete adapters.

## 2. Multi-Tenant & Security Invariants

- Every tenant table has `user_id UUID NOT NULL REFERENCES users ON DELETE CASCADE`, an index
  leading with `user_id`, and PostgreSQL RLS: `ENABLE` + `FORCE ROW LEVEL SECURITY` with the
  `tenant_isolation` policy from `app.infrastructure.db.rls`. New tables call
  `enable_tenant_rls(op, "<table>")` in their migration. No exceptions.
- Tenant context is set per transaction with `set_config('app.user_id', <uuid>, true)`
  (= `SET LOCAL`) inside `SqlAlchemyUnitOfWork`. Repositories ALSO filter by `user_id` explicitly
  (RLS is defense-in-depth, not the only guard).
- Cross-tenant access (outbox relay, cron sweeps) uses `SystemUnitOfWork` only
  (`app.system = 'on'`). Never use it from a request handler.
- The application connects as a NON-superuser role (superusers bypass RLS).
- All secrets and PII — LLM API keys, embedding keys, profile PII JSON, browser `storage_state`,
  notification addresses — are encrypted with `AesGcmCipher` (AES-256-GCM, 96-bit random nonce)
  using row-bound AAD built by `aad(table, column, user_id, row_id)` =
  `f"{table}:{column}:{user_id}:{row_id}"`, with `KeyRing` versioning (`key_version` column stored
  next to ciphertext + nonce). Row IDs are generated app-side (UUIDv7) so AAD is known pre-insert.
- API responses expose only `api_key_last4`. Raw keys are `SecretStr` / never serialized.
- The structlog redaction processor (`app.infrastructure.logging`) scrubs `nvapi-…`, `sk-…`,
  `Bearer …`, `Authorization` headers, and PII field names from every log record, including
  stdlib loggers (`httpx`, `openai`, `uvicorn`). Provider error messages pass through
  `redact_text()` before being logged, persisted, or returned.
- Outbound URLs supplied by users (custom LLM base URLs, crawl targets) go through the SSRF guard
  (`app.infrastructure.scraping.ssrf_guard`).
- Auth: Argon2id hashes; 15-min access JWT; rotating refresh tokens with token-family reuse
  detection; tokens in `httpOnly` cookies; CSRF double-submit (`csrf_token` cookie +
  `X-CSRF-Token` header) on every cookie-authenticated mutating request.
- Never submit an application without a recorded human "OK" whose `preview_hash` matches.
  Batch auto-stage (`BatchPrepare`, `POST /applications/batch-prepare`, campaign `auto_stage`)
  only fills up to `AWAITING_FINAL_VERIFICATION` / `PAUSED_NEEDS_USER_INPUT`, max 25 at a time.
  Campaign `auto_stage` is ON by default (limit 10): the runner enters phase `FILLING`, waits
  (`AutoStageCampaign`) until each of the campaign's staged applications has left the filler,
  then `AWAITING_OK` (if any is ready) and only then `COMPLETED`.
- The filler fills ONLY the application form: search, newsletter, lead-capture / counselling,
  comment and contact forms are excluded by `SCOPE_JS` and never receive student data. From an
  overview / directory page it follows at most `MAX_ENTRY_HOPS` (2) high-confidence "Apply now" /
  "Official website" links (fill and submit replay alike); the checkpoint stores the form URL.
- Campaign progress (`domain.discovery.progress.campaign_progress`) is a pure function of phase
  + counters; the runner streams it as throttled `campaign.progress` events and the API returns
  it on every campaign. Overall reaches 100% only at `COMPLETED`.
- Evidence RAG (`ProfileEvidenceRetriever`): CV/transcript `document_chunks`, essay chunks and
  memory snippets are PII-redacted (`redact_pii` with the profile's known PII values) BEFORE any
  model call; the model's answer is redacted again. Auto-fill only when model confidence x
  grounding overlap >= 0.85; otherwise the question still pauses, with the suggestion attached.
  PII fields themselves are only ever filled by the deterministic profile mapper.
- Portal sign-in: the filler never fills, asks for, or stores passwords. A visible
  `input[type=password]` blocks the run (`BLOCKED_NEEDS_HUMAN`); "Sign In in Browser & Resume"
  (`UnblockApplication`, `POST /applications/{id}/unblock`) opens the headful capture
  (`SessionKind.PORTAL`) and resumes only after the encrypted cookies are stored. Fill and submit
  runs load unexpired PORTAL/CHALLENGE sessions for the portal domain
  (`application.applications.portal_sessions`); the newer of checkpoint vs session wins per cookie.

## 3. Windows Native Asyncio & Playwright Compatibility

- Every entrypoint (`app.serve`, `app.main_worker`, `alembic/env.py`, `tests/conftest.py`) calls
  `app.infrastructure.runtime.event_loop.configure_event_loop()` before any loop is created.
  On `sys.platform == "win32"` it installs `WindowsProactorEventLoopPolicy` (Python < 3.14) so
  Playwright subprocess spawning never raises `NotImplementedError`. asyncpg runs on Proactor.
- Run code with `app.infrastructure.runtime.event_loop.run(coro)` (uses a Proactor loop factory)
  instead of bare `asyncio.run` in entrypoints.
- Start the API with `uv run serve` (not raw `uvicorn --reload`: uvicorn's reload mode forces a
  Selector loop on Windows). Start workers with `uv run worker` (not the bare `arq` CLI; arq's
  signal handlers are unsupported on Windows and are disabled there).
- Never use psycopg async (needs Selector loop on Windows); use asyncpg.

## 4. User-Provided Models Only (LLM + Embeddings)

- NEVER hardcode, guess, auto-select, probe, or silently substitute an LLM or embedding model id.
  Provider presets (`NVIDIA_NIM`, `OPENROUTER`, `CUSTOM`) carry ONLY a `base_url`; `ProviderPreset`
  has no model fields by design. The chat `model` is required and is the exact string the user
  typed or picked; every chat call (`verify`, `test-prompt`, extraction, eligibility) uses it.
- Embeddings: if the user saved an `embedding_model`, use exactly that id (sent with
  `dimensions=1024`; `input_type` only when the host is NVIDIA's). If it is blank, use the local
  deterministic `HashingEmbedding1024` — never call an external embedding model the user did not
  choose. Persist `embedding_model` on every vector row; recall only compares same-model vectors.
- The only discovery aid is `POST /api/v1/llm/fetch-models`, which lists `GET {base_url}/models`
  for the user's key so they can pick; it never selects or saves anything itself.
- `EMBEDDING_DIM = 1024`; all pgvector columns are `vector(1024)`; `ensure_dim` rejects anything
  else with `EmbeddingDimensionError` (the user then picks a different model).
- Semantic Q&A memory embeds BOTH stored and incoming questions with `embed_query` (symmetric);
  asymmetric passage/query scored an identical question at only 0.68 in live tests, below the
  0.85 auto-fill threshold. Essay chunks use passage (index) / query (search).
- Per-tenant vector search uses a `MATERIALIZED` CTE filtered by `user_id` (exact ranking); do
  not rely on HNSW iterative scans (pgvector 0.8): the embedded dev DB ships 0.6.2.
- Optional model slots `extraction_model`, `rerank_model` (+ `rerank_base_url`) and
  `vision_model` are blank by default and hold only what the user typed. Blank rerank = the local
  hybrid scorer (BM25 + opportunity signals + embedding cosine) in
  `app.domain.discovery.rag_filter`; blank vision = text/HTML/pypdf parsing only (no screenshots
  are even captured). Resolvers return `None` for a blank slot; never fall back to another id.
- `tests/unit/test_m6_units.py::test_no_model_id_is_hardcoded_in_source` scans `backend/src` and
  `frontend/src` for vendor model ids; keep it green (placeholders say "exact model id").

## 4a. LLM Call Economy (in-page RAG + pacing)

- Every outbound call on a provider key (chat, extraction, eligibility, rerank, vision) acquires
  the shared sliding-window limiter from `app.infrastructure.llm.rate_limiter.RateLimiterRegistry`
  (keyed by host + key hash; `rpm_limit` on the provider, default 36, editable live). Pace
  proactively; never rely on 429 retries for throughput.
- Pages longer than `MIN_FILTER_CHARS` go through the in-page RAG filter before extraction:
  semantic sections are scored, the lead + relevant sections are kept, and when the result fits
  `SINGLE_WINDOW_CHARS` (6000) it is extracted in ONE call. Multi-opportunity pages keep every
  relevant section (never drop a listed opportunity to save tokens). Record a `RAG_FILTER` trace
  step with `rag_sections_total`, `rag_sections_kept`, `chars_saved_pct`, `scorer`.
- PDF opportunity URLs are fetched as documents (SSRF-checked per hop, 15 MB cap), parsed by
  `IDocumentLayoutParser` (`PDF_PARSE` step); low-text pages go to the vision model only when
  the user configured one (`VISION_PARSE` step).

## 5. UI/UX Design System Mandate (Zero AI Slop)

- Neutral graphite/zinc palette defined ONLY in `frontend/src/styles/tokens.css`; components use
  tokens (Tailwind theme vars), never ad-hoc hex values.
- 1px crisp borders, small radii (≤ 4px), high-density tables, 13px base text.
- `Inter` for UI text; `JetBrains Mono` for telemetry, latencies, counts, IDs, timestamps.
- Status accents are purposeful and singular: emerald = connected / eligible / success,
  amber = action needed, crimson = failed / error. Nothing else is colored.
- Strictly NO neon or purple/blue gradients, NO marketing hero sections, NO emoji, NO decorative
  illustrations, NO glassmorphism. Mission-control density over whitespace.

## 6. Standard Verification Gate (must pass before any milestone is marked complete)

Run from `backend/`:
```
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run lint-imports
uv run pytest -q --cov
```
Run from repo root:
```
pnpm -C frontend typecheck && pnpm -C frontend lint && pnpm -C frontend build
```
Integration tests use an embedded PostgreSQL (`pgserver`, bundles pgvector) unless
`TEST_DATABASE_URL` is set, and `fakeredis` unless `TEST_REDIS_URL` is set. Report skipped tests
honestly; never mark a gate passed with failures.

## 7. Local runtime

- Start/stop everything with `scripts/launcher.py` (Desktop shortcut). Logs: `.logs/`.
- `next dev` writes `.next-dev/`, `next build` writes `.next/` (phase-based `distDir`), so the
  frontend gate may run while the dev server is live.

## 8. Conventions

- Python: `ruff` (line length 100), `mypy --strict`, full type hints, `async` everywhere for I/O.
- One use case = one class with `async def execute(...)` in `app.application.<context>`.
- Domain state changes that the UI must see write a `pipeline_events` row in the SAME transaction
  (transactional outbox); the relay publishes to Redis `events:{user_id}`; SSE replays by `seq`.
- Migrations are hand-reviewed Alembic revisions in `backend/alembic/versions/`.
- Frontend: server state via TanStack Query; validate every API payload with zod; all mutating
  fetches go through `src/lib/api.ts` (adds CSRF header, handles refresh).
