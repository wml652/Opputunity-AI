# Architecture

> Source of truth for the system design. Project rules live in `/CLAUDE.md`.
> Implementation status is tracked in the "Milestone status" section at the end.

## Context
`D:\2.0` is an empty git repo: only `.gitignore` (`.aider*`) and some aider history files, with no commits. There is no existing code to reuse, so this is a greenfield design.

**Goal:** build a multi-user, cloud-ready platform that:
1. discovers scholarships, internships and research opportunities from sites the user names, from web search, and from LinkedIn;
2. reasons about eligibility and deadlines against the student's profile;
3. drafts essays in the student's own voice;
4. fills application portals with Playwright, learning answers to custom questions over time;
5. **never submits without an explicit human "OK"**.

**Decisions already made:**
- Multi-tenant: JWT auth, `user_id` on every table.
- Runs natively with `uv` + `pnpm`, with no local Docker required. Production Dockerfiles target Railway, Render, Fly.io or a VPS.
- Redis + **arq** for jobs.
- Pluggable search: Tavily, Brave, and a zero-key `ddgs` fallback.
- Pluggable LinkedIn adapter: public/search mode, plus an optional authenticated `storage_state` mode.

**Stack:**
- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, arq, Playwright, trafilatura + BeautifulSoup, httpx, `openai` SDK (pointed at any compatible base URL), `cryptography`, argon2-cffi, PyJWT.
- **Frontend:** Next.js 15 App Router, TypeScript (strict), Tailwind, Radix UI, TanStack Query, zod.
- **Data:** PostgreSQL 16 + pgvector ≥ 0.8.
- **Local without Docker:** Postgres via a Neon or Supabase free tier, or native Postgres with pgvector. Redis via Upstash (`rediss://`), or Memurai on Windows.

**Policy guardrails built into the design** (they limit liability and keep the product honest):
- LinkedIn authenticated mode is **off by default**. Enabling it requires the user to acknowledge the ToS. It is rate-limited with jitter and has a per-day action cap.
- No CAPTCHA solving and no bot-detection bypass beyond a normal realistic browser profile. A CAPTCHA or MFA prompt moves the application to `BLOCKED_NEEDS_HUMAN`.
- The essay critic is a **quality and voice-fidelity** pass (removes clichés, matches the student's cadence). It does not try to evade AI detectors.
- When eligibility extraction finds that a portal forbids AI-generated essays, the draft is flagged, and the user must edit or author it before it can be staged.

**Approved refinements (to be written into `CLAUDE.md`):**
1. **Windows asyncio compatibility.** Every entrypoint (API `main.py`, `main_worker.py`, Alembic `env.py`, pytest `conftest.py`) calls a shared `infrastructure/runtime/event_loop.py::configure_event_loop()`. On `sys.platform == "win32"` it sets `WindowsProactorEventLoopPolicy`, so Playwright's subprocess spawning works and never raises `NotImplementedError`. asyncpg works on the Proactor loop. uvicorn is launched with `--loop asyncio` (the project's own runner script calls `configure_event_loop()` first), and arq is launched through `main_worker.py` rather than the bare `arq` CLI. This is covered by a test.
2. **Embedding resilience at 1024 dimensions.** `IEmbeddingProvider` is configured separately from chat:
   - The `embedding_*` columns on `llm_provider_configs` (`embedding_base_url`, encrypted `embedding_api_key`, `embedding_model`) are optional overrides.
   - With NVIDIA NIM configured, the default is `nvidia/nemotron-3.5-embed-1b`, requested at `dimensions=1024` (`input_type` query/passage). This replaces the originally specified `nvidia/nv-embedqa-e5-v5`, which NVIDIA retired (HTTP 410).
   - Otherwise the provider uses the explicit embedding endpoint if one is set. If none is set, it falls back to a deterministic local `HashingEmbedding1024` adapter (feature hashing, L2-normalized) and logs a degraded-quality warning.
   - A dimension guard raises an error if any provider returns a vector that is not 1024-dimensional.
   - Switching the chat provider (e.g. to OpenRouter) never changes the embedding space; `embedding_model` is stored per row.

---

## 0. Execution Order After Approval
1. Create the root **`CLAUDE.md`** constitution covering:
   - the hexagonal import laws, enforced by import-linter;
   - the tenant and RLS invariants;
   - the AesGcmCipher + AAD (`f"{table}:{column}:{user_id}:{row_id}"`) + KeyRing rule for secrets and PII;
   - structlog redaction (`nvapi-`, `sk-`, `Bearer`, PII keys);
   - the two refinements above;
   - the UI mandate: graphite/zinc tokens, 1 px borders, Inter + JetBrains Mono, emerald/amber/crimson status accents only, no gradients, hero sections or emoji;
   - the standard gate commands.
2. Implement **Milestone 1** as scoped in §7. Runtime services (Postgres + pgvector, Redis) are needed for the integration tests. If they are not reachable locally, those tests run against `TEST_DATABASE_URL`/`TEST_REDIS_URL`, or fall back to clearly-marked skips with fakes (fakeredis), and any skips are reported honestly.
3. Run the M1 gate (§7), then report: the files created, the test results, and the exact commands to verify a real NVIDIA NIM key in the UI.

---

## 1. Monorepo Directory Structure

```
/
├── backend/
│   ├── pyproject.toml               # uv; ruff, mypy --strict, pytest, import-linter config
│   ├── alembic/ (env.py, versions/)
│   ├── Dockerfile.api  Dockerfile.worker   # worker image = playwright base
│   ├── src/app/
│   │   ├── domain/                  # pure Python, zero framework imports
│   │   │   ├── shared/              # Entity base, UserId, Clock port, DomainEvent, errors
│   │   │   ├── identity/            # User
│   │   │   ├── llm/                 # ProviderConfig, ProviderKind, ConnectionStatus
│   │   │   ├── profile/             # StudentProfile, Document, ReferenceEssay, value objects (GPA, DegreeLevel)
│   │   │   ├── memory/              # KnowledgeQA, ConfidenceScore (threshold 0.85)
│   │   │   ├── discovery/           # Campaign, Source, Scope, Opportunity, Deadline
│   │   │   ├── eligibility/         # EligibilityRule, Verdict, deterministic rule evaluators
│   │   │   ├── essay/               # StyleFingerprint, EssayDraft, VoiceScore
│   │   │   ├── application/         # Application aggregate + state_machine.py (transition table)
│   │   │   ├── notification/        # Notification, Channel
│   │   │   └── ports/               # Protocols: ILLMProvider, IEmbeddingProvider, ISearchProvider,
│   │   │                            #   ISourceCrawler, ILinkedInAdapter, IBrowserSession, IFormFiller,
│   │   │                            #   INotifier, IObjectStorage, ICipher, IEventBus, IJobQueue,
│   │   │                            #   repositories (I*Repository), IUnitOfWork
│   │   ├── application/             # use cases (one class per command/query), DTOs, orchestration
│   │   │   ├── llm/ (configure_provider, verify_connection)
│   │   │   ├── profile/ memory/ discovery/ eligibility/ essay/ applications/ notifications/
│   │   │   └── services/ (field_mapper, prompt_guard, priority_scheduler)
│   │   ├── infrastructure/
│   │   │   ├── config/ (pydantic-settings)
│   │   │   ├── db/ (models.py, session.py w/ RLS SET LOCAL, repositories/, uow.py)
│   │   │   ├── crypto/ (aes_gcm_cipher.py, key_ring.py)
│   │   │   ├── llm/ (openai_compatible.py, presets: nvidia_nim, openrouter, custom)
│   │   │   ├── search/ (tavily.py, brave.py, ddgs.py, chained.py)
│   │   │   ├── scraping/ (ssrf_guard.py, playwright_pool.py, distiller.py, sanitizer.py, site_crawler.py)
│   │   │   ├── linkedin/ (public_adapter.py, authenticated_adapter.py)
│   │   │   ├── browser/ (session_store.py, form_filler.py, dom_snapshot.py, checkpoint.py)
│   │   │   ├── stylometry/ (metrics.py — pure NLP features)
│   │   │   ├── notify/ (smtp.py, resend.py, twilio_whatsapp.py, meta_whatsapp.py, dispatcher.py)
│   │   │   ├── storage/ (local_fs.py, s3.py — encrypted blobs)
│   │   │   ├── events/ (outbox_relay.py, redis_bus.py)
│   │   │   └── workers/ (arq settings + task modules: discovery, eligibility, essay, browser, notify, outbox)
│   │   ├── api/
│   │   │   ├── main.py  deps.py (current_user, uow)  errors.py  middleware/ (request-id, log redaction)
│   │   │   └── v1/ (auth, llm, profile, documents, essays, memory, campaigns, opportunities,
│   │   │           applications, inbox, events_sse, webhooks)
│   │   └── main_worker.py
│   └── tests/ (unit/ integration/ e2e/ fixtures/fake_portal/)
├── frontend/
│   ├── package.json  Dockerfile  tsconfig.json (strict)
│   └── src/
│       ├── app/ (auth)/login  (console)/{layout.tsx, page.tsx=pipeline, inbox/, campaigns/[id]/,
│       │         opportunities/, applications/[id]/, vault/{profile,documents,essays,memory}/, settings/llm/}
│       ├── components/ ui/ (Radix wrappers)  telemetry/  pipeline/  inbox/  vault/  verification-drawer/
│       ├── lib/ api-client.ts (typed, generated from OpenAPI via openapi-typescript)  sse.ts  auth.ts
│       └── styles/ tokens.css (neutral graphite palette, single accent, mono numerics)
├── docs/ARCHITECTURE.md  .env.example  README.md (native + cloud runbooks)
```

**Layering is enforced by `import-linter` contracts.** `domain` imports nothing internal. `application` may import `domain` only. `infrastructure` and `api` may import both, but never each other's internals. Wiring happens in a composition root, `api/deps.py` and the worker `startup`.

---

## 2. PostgreSQL + pgvector Schema

Conventions shared by every table:
- `id UUID PK` (uuid7), `created_at`, `updated_at`.
- **`user_id UUID NOT NULL FK users ON DELETE CASCADE`** on every tenant table, plus a composite index starting with `user_id`.
- **Row-level security** (RLS) is enabled on every tenant table with the policy `user_id = current_setting('app.user_id')::uuid`. The session does `SET LOCAL app.user_id` per transaction. This is defense-in-depth on top of repository scoping.
- `version INT` for optimistic locking on aggregates.

**Enums**
- `provider_kind`: NVIDIA_NIM, OPENROUTER, CUSTOM
- `scope`: LOCAL, INTERNATIONAL, BOTH
- `source_type`: WEBSITE, WEB_SEARCH, LINKEDIN_PUBLIC, LINKEDIN_AUTH
- `campaign_status`: DRAFT, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED
- `crawl_status`: PENDING, FETCHING, DISTILLED, EXTRACTED, BLOCKED_SSRF, FAILED
- `opportunity_type`: SCHOLARSHIP, INTERNSHIP, RESEARCH, REMOTE_JOB, OTHER
- `eligibility_outcome`: ELIGIBLE, INELIGIBLE, EXPIRED, UNCERTAIN
- `application_state`: see §3
- `question_status`: OPEN, ANSWERED, DISMISSED
- `doc_kind`: CV, TRANSCRIPT, ID, CERTIFICATE, OTHER
- `notif_channel`: EMAIL, WHATSAPP
- `notif_status`: QUEUED, SENT, DELIVERED, FAILED
- `essay_status`: DRAFT, CRITIQUED, USER_EDITED, APPROVED

| Table | Key columns | Notes |
|---|---|---|
| `users` | email (citext unique), password_hash (argon2id), display_name, timezone, is_active | not RLS-scoped |
| `refresh_tokens` | user_id, token_hash, family_id, expires_at, revoked_at | rotation + reuse detection |
| `llm_provider_configs` | user_id, kind, label, base_url, model, embedding_model, **api_key_ciphertext BYTEA, api_key_nonce, key_version**, api_key_last4, is_active | partial unique (user_id) WHERE is_active |
| `llm_connection_checks` | user_id, provider_config_id, ok, latency_ms, error_code, checked_at | feeds telemetry badge |
| `student_profiles` | user_id (unique), full_name, **pii_ciphertext** (address, phone, DOB, national ID: encrypted JSON), degree_level, major, gpa NUMERIC(3,2), gpa_scale, nationality, country_of_residence, graduation_year, skills TEXT[], languages JSONB, experience JSONB, extra JSONB | non-PII columns stay queryable for eligibility |
| `documents` | user_id, kind, filename, mime, size, sha256, storage_key, encrypted, extracted_text (for mapping) | blobs encrypted in object storage |
| `reference_essays` | user_id, title, body, word_count, prompt_context | 4–5 per user |
| `essay_chunks` | user_id, essay_id, ordinal, text, **embedding vector(1024)** | few-shot retrieval; HNSW cosine |
| `style_fingerprints` | user_id, version, metrics JSONB (sentence len mean/σ/CV, burstiness, MTLD, TTR, function-word freq, punctuation profile, transitions[], paragraph cadence, readability), tone_summary, feature_vector REAL[] | latest is active |
| `knowledge_memory_qa` | user_id, question_text, question_normalized, answer_text, **embedding vector(1024)**, embedding_model, source (USER/PROFILE/INFERRED), times_reused, last_used_at, opportunity_id NULL | HNSW `vector_cosine_ops`; queries `WHERE user_id=$1 ORDER BY embedding <=> $2` (pgvector 0.8 iterative scan) |
| `campaigns` | user_id, name, scope, keywords TEXT[], status, started_at, finished_at, counters JSONB (cached) | |
| `campaign_sources` | user_id, campaign_id, source_type, url NULL, enabled | |
| `crawled_pages` | user_id, campaign_id, source_id, url, final_url, http_status, status, content_sha256, markdown TEXT, injection_flags JSONB, fetched_at | unique (campaign_id, content_sha256) |
| `opportunities` | user_id, campaign_id, page_id, type, title, organization, location, is_international, deadline_at TIMESTAMPTZ NULL, deadline_raw, portal_url, eligibility_rules JSONB, ai_policy (ALLOWED/PROHIBITED/UNKNOWN), dedupe_key, **embedding vector(1024)** | unique (user_id, dedupe_key); semantic dedupe |
| `eligibility_verdicts` | user_id, opportunity_id, outcome, reasons JSONB (per-rule pass/fail/unknown + evidence), evaluated_at, profile_snapshot_hash, model | history kept; latest pointer on opportunity |
| `applications` | user_id, opportunity_id (unique per user), state, priority_rank, deadline_at (denorm), attempt, lock_owner, lease_expires_at, last_error, submitted_at, version | index (user_id, state, deadline_at) |
| `application_transitions` | user_id, application_id, from_state, to_state, actor (WORKER/USER/SYSTEM), reason, metadata, at | append-only audit |
| `application_checkpoints` | user_id, application_id, seq, url, step_index, dom_fingerprint, **storage_state_ciphertext**, fill_ledger JSONB, screenshot_key | latest = resume point |
| `application_fields` | user_id, application_id, step_index, selector, label, field_type, options JSONB, value, value_source (PROFILE/MEMORY/ESSAY/DOCUMENT/USER), confidence, memory_qa_id NULL, user_edited | the field-by-field preview |
| `pending_questions` | user_id, application_id, field_id, question_text, context, suggested_answer, confidence, status, answered_qa_id, answered_at | Action Inbox |
| `essay_drafts` | user_id, application_id NULL, prompt, word_limit, body, critique JSONB, voice_match_score, burstiness_score, status, fingerprint_version | |
| `final_verifications` | user_id, application_id, screenshot_key, preview_hash, approved_at, approved_preview_hash, receipt_screenshot_key, confirmation_text | approval bound to a hash |
| `browser_sessions` | user_id, domain, kind (PORTAL/LINKEDIN), **storage_state_ciphertext**, tos_acknowledged_at, last_used_at, expires_at | |
| `notification_channels` | user_id, channel, address_ciphertext, verified_at, enabled | |
| `notifications` | user_id, channel, template, payload JSONB, status, provider_message_id, error, related_application_id | |
| `pipeline_events` | user_id, campaign_id NULL, application_id NULL, type, payload JSONB, seq BIGSERIAL, published_at NULL | **transactional outbox** + SSE replay |
| `audit_log` | user_id, actor, action, entity, entity_id, ip, at | security-relevant actions |

**Embedding dimension.** The dimension is fixed at 1024 (e.g. `nvidia/nemotron-3.5-embed-1b` at `dimensions=1024`). `IEmbeddingProvider` validates it at startup. If the embedding model changes, an admin re-embed job rebuilds the vectors; `embedding_model` is stored per row.

---

## 3. State Machine & Worker Orchestration

### Application states (`domain/application/state_machine.py`: an explicit transition table; illegal transitions raise errors)
```
DISCOVERED → ELIGIBILITY_PENDING → {INELIGIBLE | EXPIRED | ELIGIBLE}
ELIGIBLE → QUEUED (priority = deadline ASC)
QUEUED → PREPARING (essays drafted, documents resolved) → FILLING
FILLING → PAUSED_NEEDS_USER_INPUT      (unknown field, confidence < 0.85)
PAUSED_NEEDS_USER_INPUT → RESUME_QUEUED (all open questions answered) → FILLING
FILLING → BLOCKED_NEEDS_HUMAN           (captcha / MFA / login wall) → RESUME_QUEUED
FILLING → AWAITING_FINAL_VERIFICATION   (reached submit step; screenshot + preview saved)
AWAITING_FINAL_VERIFICATION → SUBMIT_APPROVED (user "OK", preview_hash matched) → SUBMITTING
SUBMITTING → SUBMITTED | SUBMISSION_FAILED (→ retry → RESUME_QUEUED, max N)
any non-terminal → EXPIRED (deadline sweep) | CANCELLED (user) | FAILED (non-retryable)
```
Every transition runs in **one database transaction**: an optimistic `version` check, a row in `application_transitions`, and a row in `pipeline_events` (the outbox).

### arq queues and workers
- `discovery`: crawl sources, search, LinkedIn public mode, distill, extract.
- `reasoning`: eligibility and essays (LLM-bound).
- `browser`: fill, resume and submit. Its concurrency is capped per worker, plus a per-user semaphore enforced with a Redis lock.
- `notify`: email and WhatsApp.
- `system` cron jobs:
  - the outbox relay, as a fallback; the primary path uses Postgres `LISTEN/NOTIFY`;
  - the deadline sweep, which expires paused or queued applications;
  - lease reaping, which re-queues applications whose `lease_expires_at` has passed;
  - the priority scheduler, which picks the next `QUEUED` application ordered by `deadline_at ASC` per user.

### Pause and resume without losing session state
Browsers are **not** held open while waiting on a human (that could take hours or days, and it cannot scale across tenants). Instead, the **checkpoint and deterministic replay** model is used:
1. **Checkpoint after every form step.** The filler persists an `application_checkpoints` row containing:
   - the encrypted `context.storage_state()` (cookies and localStorage);
   - the current URL and step index;
   - a DOM fingerprint (a hash of the normalized form-field signature);
   - the `fill_ledger` (an ordered list of `{step, selector, action, value_ref}`);
   - a screenshot.
2. **Pause.** On an unresolved field, the filler finishes the rest of that step that it can answer and saves the checkpoint. It then creates a `pending_questions` row for each unknown, moves to `PAUSED_NEEDS_USER_INPUT`, releases its lease, closes the context, and enqueues `notify`.
3. **Answer.** The `POST /inbox/questions/{id}/answer` use case:
   - writes `knowledge_memory_qa` (text plus embedding);
   - links the question;
   - checks whether the application's open questions are all resolved, and if so transitions to `RESUME_QUEUED` and enqueues `browser.resume`.
4. **Resume.** A fresh context is created with the saved `storage_state`, and the browser navigates to the checkpoint URL.
   - **If the DOM fingerprint matches**, the filler continues from `step_index`.
   - **If it does not** (the portal lost its server-side draft), the filler replays the `fill_ledger` from step 0. Values are resolved by reference (`value_ref` points to `application_fields`), so user edits apply automatically. Each step's fingerprint is re-checked.
   - **If the replay diverges**, the application goes to `BLOCKED_NEEDS_HUMAN` with a diff.
5. **Hot hold (optional optimization).** If an answer arrives within a short time-to-live (≈10 min) while the worker still holds the context, it is delivered through a Redis key and the filler continues in-process.
6. **Final gate.**
   - At the submit step, the filler captures a full-page screenshot and computes `preview_hash = sha256(canonical(application_fields))`. It then saves the checkpoint, moves to `AWAITING_FINAL_VERIFICATION`, and notifies the user.
   - The user's "OK — Verify & Submit" request carries field edits and the `preview_hash` they saw. The server applies the edits, recomputes the hash, records the approval in `final_verifications`, and transitions to `SUBMIT_APPROVED`.
   - The worker resumes, re-fills any edited fields, and **re-verifies that the on-page values equal the approved values** before clicking submit. On any mismatch it re-stages instead of submitting. It then captures the receipt screenshot and confirmation text.
7. **Leases and idempotency.**
   - Each browser job takes `lock:app:{id}` in Redis and sets `lease_expires_at`, with a heartbeat every 30 s.
   - The submit job is idempotent: it is a no-op if `final_verifications.receipt_screenshot_key` is already set or the state is past `SUBMITTING`.
   - `SUBMITTING` is never auto-retried blindly. On a crash mid-submit, the worker inspects the page for a confirmation signal before deciding.

### Field mapping (`application/services/field_mapper.py`)
Each field is resolved in this order:
1. A deterministic profile mapping (label synonyms mapped to profile paths, e.g. "Phone" → `pii.phone`).
2. A document upload slot (keyword matching to `doc_kind`).
3. An essay prompt detector: long textarea plus a prompt, routed to the essay engine.
4. Semantic memory: embed the normalized question, take the top-k by cosine similarity, then let the LLM adjudicate whether the stored answer fits. Confidence is `min(similarity, llm_confidence)`.

A field with confidence ≥ 0.85 is auto-filled; anything lower is escalated. Select and radio fields are always constrained to the options the portal actually offers.

### Live streaming
- Use cases write `pipeline_events` rows in the same transaction as the state change.
- The outbox relay (`LISTEN/NOTIFY` wake-up) publishes them to the Redis channel `events:{user_id}` and marks them `published_at`.
- `GET /api/v1/events/stream` is SSE, authenticated by cookie. It replays from `Last-Event-ID` (the `seq` column) and then tails Redis pub/sub, with a heartbeat every 15 s.
- The frontend `useEventStream` hook patches TanStack Query caches: counters, the phase tracker and the inbox badge.

---

## 4. Security & Notification Architecture

**Auth and tenancy**
- argon2id password hashes.
- Access JWT (15 min, EdDSA/HS256), plus a rotating refresh token in an httpOnly, Secure, SameSite=Lax cookie, with reuse detection by token family.
- CSRF double-submit token on mutating requests.
- `current_user` dependency → `UnitOfWork(user_id)` → `SET LOCAL app.user_id`. Every repository method requires the `user_id`.
- An integration test asserts that user A cannot read or modify any of user B's rows, through either the API or raw SQL under RLS.
- Rate limiting via Redis (SlowAPI or a custom middleware) on auth, verify and answer endpoints.

**Encryption at rest** (`infrastructure/crypto`)
- `AesGcmCipher` uses AES-256-GCM with a 96-bit random nonce. The AAD is `f"{table}:{column}:{user_id}:{row_id}"`, which binds a ciphertext to its row and prevents swapping ciphertexts between rows.
- A `KeyRing` holds versioned keys (`ENCRYPTION_KEYS=v2:base64,v1:base64`). Reads use any key version; writes use the newest. A rotation job re-encrypts old rows.
- The design is ready for cloud KMS: the master key can be swapped for a KMS-wrapped data key.
- Encrypted fields: API keys, PII, `storage_state`, notification addresses, and document blobs.
- API responses expose only `api_key_last4`.
- A structlog redaction processor scrubs `sk-`/`nvapi-` patterns, `Authorization` headers and PII keys. Pydantic `SecretStr` is used end-to-end.

**LLM gateway**
- `OpenAICompatibleProvider(base_url, api_key, model)` wraps `AsyncOpenAI` with a timeout, retries (tenacity, honouring 429/5xx and `Retry-After`) and JSON-schema structured output, falling back to JSON mode plus Pydantic validation.
- `POST /llm/verify` sends a minimal chat completion (`max_tokens=1`) and a `/models` probe, measures latency with `perf_counter`, persists `llm_connection_checks`, and emits a `llm.status` SSE event. The telemetry bar shows "Connection Established · 312 ms · nvidia/llama-3.3-70b".
- **SSRF guard on custom `base_url`s as well:** HTTPS is required in production.

**SSRF guard** (`scraping/ssrf_guard.py`)
- Only `http`/`https` schemes on ports 80, 443 or an allowlist.
- The hostname is resolved and **every** resolved IP is rejected if it is private, loopback, link-local, CGNAT, multicast or reserved, including the IPv4-mapped IPv6 forms and `169.254.169.254`/`fd00:ec2::254`.
- Inside Playwright, `context.route("**/*")` validates every request (navigations, redirects and subresources) against the guard.
- httpx uses a custom transport that re-validates on each redirect and pins the resolved IP.
- Blocked pages are recorded as `crawl_status=BLOCKED_SSRF`.

**Indirect prompt injection**
- The `sanitizer` strips scripts, hidden elements (`display:none`, zero-size, off-screen, aria-hidden), comments and zero-width or bidirectional-control characters, then normalizes Unicode (NFKC). It flags instruction-like patterns ("ignore previous", "system:", tool-call syntax) into `injection_flags`.
- Untrusted text is always placed inside a delimited `<untrusted_document>` block. The system prompt states that it is data, never instructions.
- Every LLM output is **schema-constrained** (Pydantic). The model cannot invoke tools or choose URLs freely.
- Navigation during filling is restricted to the portal's registrable domain plus a known identity-provider allowlist.
- PII is only ever written into fields whose mapping came from the deterministic profile mapper or a user-approved memory entry, never because the LLM was told to by page content.
- The human "OK" gate is the final backstop.

**Browser hygiene**
- Isolated contexts per application.
- A realistic, consistent user agent, viewport and locale (no fingerprint spoofing beyond that).
- Downloads are disabled, except for confirmations.
- File uploads come only from the user's own decrypted documents, written to a temp directory that is deleted afterwards.

**Notifications** (`domain/ports/INotifier`; `infrastructure/notify/dispatcher.py` fans out to the user's enabled channels)
- **Email:** `SmtpNotifier` (aiosmtplib) and `ResendNotifier` (HTTP), with Jinja templates for `question_needed`, `ready_for_ok`, `submitted` and `failed`.
- **WhatsApp:**
  - `TwilioWhatsAppNotifier`, and `MetaWhatsAppNotifier` (Cloud API, pre-approved **template messages** because messages are business-initiated outside the 24 h window).
  - Status webhooks at `/webhooks/twilio` and `/webhooks/meta` verify signatures (`X-Twilio-Signature`, `X-Hub-Signature-256`) and update `notifications.status`.
- Messages contain **no PII and no one-tap submit**. They carry a deep link (`/inbox?app=…`) that requires an authenticated session.
- Delivery happens on the `notify` queue with retries and deduplication (`template + application_id + state`), so the same alert is never sent twice.
- Channel verification uses a one-time code before a channel can be used.

---

## 5. Discovery, Eligibility & Essay Engines (key mechanics)

**Discovery**
- A campaign fans out per source into `discovery.crawl_source` jobs. Sources run sequentially within a domain, with per-domain politeness (`robots.txt` respected, 1–3 s jitter) and in parallel across domains.
- **Website sources:** BFS limited to depth 2 and N pages, following only links scored relevant by keyword or URL heuristics. Pages go through Playwright, then trafilatura to Markdown (BeautifulSoup as fallback), then the sanitizer.
- **Web search:** `ChainedSearchProvider`, [Tavily → Brave → ddgs], with queries built from keyword × scope templates (e.g. `"bachelor scholarship" international 2027 deadline`).
- **LinkedIn:**
  - Public mode uses the guest jobs search URLs, rate-limited.
  - Authenticated mode uses the stored `storage_state`, is opt-in, has a daily cap, and is used only for the user's own Easy Apply flows.
- **Extraction:** the LLM returns `list[OpportunityExtract]` (title, organization, type, deadline in ISO plus the raw text, eligibility criteria as structured rules, portal URL, AI policy).
- **Deduplication:** a normalized `dedupe_key` (organization + title + deadline), then embedding similarity > 0.92 merges duplicates.

**Eligibility**
- Deterministic rules run first: deadline vs. `now(UTC)`, degree level, GPA (normalized to the scale), nationality/residency lists, and graduation-year window. The LLM handles only the fuzzy criteria (e.g. "students from developing countries"), returning per-criterion evidence.
- Outcome: any hard fail → INELIGIBLE or EXPIRED; any unresolved fuzzy criterion → UNCERTAIN (shown to the user, who can include it manually). The full reasoning is persisted.
- Eligible items are inserted into `applications` as QUEUED with `priority_rank` ordered by deadline (nulls last).

**Stylometric essays**
- *Stage 1 (deterministic plus LLM):* `stylometry/metrics.py` computes:
  - sentence-length mean/σ/CV (burstiness = σ/μ);
  - MTLD/TTR lexical diversity;
  - function-word frequencies;
  - a punctuation profile;
  - the transition-phrase inventory;
  - paragraph length cadence;
  - readability.
  An LLM then summarizes tone and recurring rhetorical moves. Everything is stored as a versioned `style_fingerprints` row.
- *Stage 2:* retrieve the top-k `essay_chunks` by similarity to the prompt and pull matching profile facts. Generate the draft with an explicit style spec (target σ of sentence length, the student's transitions, banned-phrase list) and the word limit. Then the critic:
  1. a lexical cliché scan ("delve", "tapestry", "testament to", …);
  2. an LLM revise pass;
  3. a recomputation of metrics on the draft.
- **Voice Match Score:** cosine similarity between the draft's and the fingerprint's normalized feature vectors. The **Burstiness Score** is `1 − |CV_draft − CV_student| / CV_student`, clamped to [0, 1].
- **Facts guard:** every factual claim must trace back to the profile or the essays. Unsupported claims are flagged in `critique`.
- The draft is always user-reviewable. If `ai_policy=PROHIBITED`, it is blocked from staging until the user edits it.

---

## 6. Frontend "Mission Control" UI
- **Visual system:**
  - neutral graphite/zinc surfaces, light and dark themes;
  - a single restrained accent (e.g. amber for "needs you"), with green and red only for status;
  - Inter for UI text and JetBrains Mono for numbers and IDs;
  - 13–14 px base, dense tables, 1 px borders;
  - no gradients, no hero sections, no emoji;
  - Radix primitives (Dialog, Drawer via Dialog, Tabs, Select, Tooltip, Toast).
- **Top Telemetry Bar** (in the console layout): provider selector, connection badge (state dot, "Connection Established", latency in ms, model), active-worker count and queue depth (from the `/system/workers` endpoint plus SSE), and inbox count.
- **Pipeline page:**
  - six counter tiles: Websites Crawled, Opportunities Found, Eligible, Needs Input, Ready for OK, Submitted;
  - a campaign phase tracker (Crawling → Extracting → Evaluating → Preparing → Filling → Awaiting OK → Submitted) with the current step highlighted;
  - a live event log (virtualized) and an applications table sorted by deadline.
- **Action Inbox:** two tabs, *Questions* and *Ready for OK*.
  - **Questions:** each question with portal context, the suggested answer and its confidence, an answer box, and a "Save to memory" toggle (on by default).
  - **Ready for OK:** opens the **Verification Drawer**. It shows the full-page screenshot (zoomable, scrollable) on the left and the editable field and essay list on the right, with source tags (PROFILE/MEMORY/ESSAY/USER) and a diff highlight on edits. The "OK — Verify & Submit" button sends the `preview_hash`; there is also "Reject / Cancel".
- **Vault:**
  - Profile forms (react-hook-form + zod).
  - Documents: drag-and-drop PDF upload, with MIME and magic-byte checks and a size limit.
  - Essays: 4–5 slots, with the fingerprint metrics shown as small multiples.
  - Memory: a searchable table with edit and delete; semantic search mode.
- **Settings → LLM:** presets (NIM, OpenRouter, Custom), base URL, model, embedding model, masked key, "Verify connection".

---

## 7. Five-Milestone Roadmap (each ends with a verification gate)

The **standard gate** that every milestone must pass:
- `uv run ruff check . && uv run ruff format --check .`
- `uv run mypy --strict src`
- `uv run lint-imports` (hexagonal contracts)
- `uv run pytest -q --cov`
- `pnpm -C frontend typecheck && pnpm -C frontend lint && pnpm -C frontend build`

**M1: Foundation, Identity, Crypto, LLM Gateway, Event Spine**
- **Scope:**
  - monorepo scaffold, tooling, settings, `.env.example`;
  - Alembic baseline with all enums, `users`/`refresh_tokens`/`llm_*`/`pipeline_events`/`audit_log`, and RLS helpers;
  - JWT auth (register, login, refresh, logout);
  - `AesGcmCipher` and `KeyRing`;
  - LLM provider CRUD and `/llm/verify`;
  - outbox relay, Redis bus and the SSE endpoint;
  - arq worker skeleton;
  - frontend auth, console layout, Telemetry Bar and LLM settings page.
- **Gate tests:**
  - cipher round-trip, plus tamper and AAD-mismatch failures;
  - key rotation;
  - JWT refresh-reuse detection;
  - **tenant isolation** (API + RLS);
  - `/llm/verify` against a respx-mocked OpenAI endpoint (success, 401, timeout);
  - no raw key in any response or log (caplog assertion);
  - SSE replay from `Last-Event-ID`;
  - manual check: verify a real NIM or OpenRouter key and confirm the badge shows latency.

**M2: Student Vault & Semantic Memory**
- **Scope:**
  - profile (encrypted PII), documents (encrypted object storage, PDF text extraction with pypdf), reference essays and chunking;
  - `IEmbeddingProvider` and the pgvector HNSW indexes;
  - `knowledge_memory_qa` CRUD and the semantic search use case;
  - Stage 1 stylometry (fingerprint);
  - Vault UI pages.
- **Gate tests:**
  - stylometry metrics against hand-computed fixtures;
  - memory recall (an embedding stub with controlled vectors checks threshold behaviour at 0.85);
  - PDF validation rejects spoofed MIME types;
  - integration test against a real pgvector database (a testcontainers-free option: a `TEST_DATABASE_URL` pointing at a Neon branch or local PG).

**M3: Discovery & Eligibility**
- **Scope:**
  - campaigns and sources;
  - SSRF guard, the Playwright pool, distiller and sanitizer;
  - website crawler;
  - search adapters (Tavily, Brave, ddgs, chained);
  - LinkedIn public adapter;
  - LLM extraction and deduplication;
  - eligibility engine (deterministic + LLM) and the priority queue;
  - Pipeline Visualizer with live counters.
- **Gate tests:**
  - SSRF table tests (private, IPv6-mapped and metadata addresses, DNS-rebinding redirect);
  - sanitizer strips hidden prompt-injection text from fixture HTML;
  - crawler run against local fixture sites (pytest-served HTML);
  - eligibility rule matrix (GPA scales, expired deadlines, nationality);
  - priority ordering;
  - end-to-end campaign on fixtures streams the correct counter events.

**M4: Essay Synthesis, Form Filler, Question Escalation, Notifications**
- **Scope:**
  - Stage 2 essay synthesis, the critic and the scores;
  - Playwright form filler with field mapper, checkpoints and ledger replay;
  - `PAUSED_NEEDS_USER_INPUT` flow, the Inbox Questions tab and answer → memory → resume;
  - SMTP/Resend and Twilio/Meta notifiers, webhooks and channel verification.
- **Gate tests:**
  - **fake multi-step portal fixture** (`tests/fixtures/fake_portal`, a FastAPI app with three steps, custom questions, a file upload and a server-side draft that can be wiped);
  - the filler pauses on an unknown question, resumes after the answer, **and auto-answers the same question on a second application** (the memory learning loop);
  - ledger replay after a draft wipe;
  - state-machine illegal-transition tests;
  - notifier webhook signature verification;
  - essay scores are deterministic for fixed inputs.

**M5: Final Verification Gate, Submission, LinkedIn Auth, Production Hardening**
- **Scope:**
  - `AWAITING_FINAL_VERIFICATION`: screenshot, preview hash, the Verification Drawer with edits, the OK button, submission, receipt;
  - deadline sweep and lease reaper;
  - LinkedIn authenticated adapter (opt-in, ToS acknowledgement, caps);
  - Dockerfiles (API, worker on the Playwright base image, frontend standalone), `fly.toml`/`render.yaml` examples;
  - health and readiness endpoints, structured logs, OpenTelemetry hooks;
  - rate limits, CSP and security headers;
  - README runbooks (native Windows/macOS/Linux and cloud).
- **Gate tests:**
  - the fake portal is **never submitted without approval**;
  - a stale or mismatched `preview_hash` is rejected;
  - edits are applied and verified on the page before submit;
  - crash during `SUBMITTING` does not double-submit;
  - deadline expiry of paused applications;
  - Playwright e2e of the frontend (login → campaign → answer question → OK → submitted) against the fake portal;
  - `docker build` for all three images;
  - a `/security-review` pass.

---

## Verification (end-to-end, after M5)
1. Run natively:
   - `uv sync && uv run alembic upgrade head`
   - `uv run uvicorn app.api.main:app` and `uv run arq app.main_worker.WorkerSettings`
   - `pnpm -C frontend dev`
   with a Neon/Supabase `DATABASE_URL` and an Upstash `REDIS_URL`.
2. Register, configure an NVIDIA NIM key, and verify. Expect the green "Connection Established" badge with latency.
3. Fill the profile, upload a CV and 4 essays, and check that the fingerprint metrics render.
4. Run a campaign against the local fake portal's listing page plus one real public site. Watch the counters update live over SSE.
5. Answer the escalated question in the Inbox and receive the email/WhatsApp alerts (Twilio sandbox). Run a second fake application and confirm it auto-answers from memory.
6. Open the Verification Drawer, edit one field, press OK, and confirm the receipt screenshot is stored and the state is `SUBMITTED`.
7. Run the full standard gate plus the backend and frontend e2e suites in CI (GitHub Actions: PG+pgvector and Redis service containers in CI only).

---

## Milestone status

### M1: Foundation, Identity, Crypto, LLM Gateway, Event Spine (complete)

**Deviations from the original blueprint, made during M1:**

- **No `openai` SDK.** SDK 3.x moved its transport to `httpx2`, which `respx` can't mock.
  `infrastructure/llm/http_client.py` is a small typed `httpx` client covering
  `/chat/completions`, `/models` and `/embeddings`, with explicit retry and `Retry-After`
  handling plus error redaction.
- **Access token is an httpOnly cookie** (path `/api`) rather than a JS-held bearer token, so
  `EventSource` can authenticate the SSE stream. `Authorization: Bearer` is still accepted for
  API clients, and those requests are exempt from CSRF.
- **SSE frames carry no `event:` field.** Each frame is a default `message` with its `type` in
  the JSON payload, so the client never silently drops event types it doesn't know yet.
- **The embedded test Postgres (`pgserver`) ships pgvector 0.6.2.** HNSW works on it, but
  iterative index scans (0.8) don't exist. M2 must not depend on `hnsw.iterative_scan`. It
  should over-fetch with `ef_search` instead, or run vector tests against `TEST_DATABASE_URL`.
- **`/api/v1/inbox/summary` returns zeros** until M4 adds `pending_questions` and
  `final_verifications`.
- **NVIDIA retired two of the planned defaults** (both return HTTP 410 Gone, verified against
  the live API on 2026-10-28):
  - chat `meta/llama-3.3-70b-instruct` is replaced by `nvidia/nemotron-3-super-120b-a12b`;
  - embeddings `nvidia/nv-embedqa-e5-v5` is replaced by `nvidia/nemotron-3.5-embed-1b`
    with `dimensions=1024`.

  HTTP 410 maps to `model_not_found`.
- **The typed frontend client is hand-written with zod.** OpenAPI codegen is deferred to M2.

### M2: Student Vault, Stage 1 Stylometry & Semantic Memory (complete)

**Delivered:**
- Migration `0002_student_vault_and_memory`: six RLS-forced tables, `vector(1024)` columns, and
  HNSW cosine indexes.
- Profile with AES-256-GCM row-bound PII.
- Encrypted PDF vault: magic-byte check, 10 MB limit, `pypdf` extraction. Both the blob and the
  extracted text are encrypted.
- Reference essays with paragraph chunking, embeddings, and auto-versioned stylometric
  fingerprints.
- Semantic memory with recall at the 0.85 threshold.
- The four Vault UI pages.
- The one-click launcher (`scripts/launcher.py`, Desktop shortcut).

**Findings and deviations (verified with the user's live NVIDIA key):**
- **Embedding default.** `nvidia/llama-3.2-nv-embedqa-1b-v1` is listed in the catalog but returns
  404 for the account; `nv-embedqa-e5-v5` and the `-v2` variants return 410. The default is now
  `nvidia/nemotron-3.5-embed-1b` at `dimensions=1024`. It is the only probed model that returns
  exactly 1024 dimensions for this key.
- **Memory embeds symmetrically.** Questions are embedded with `input_type=query` on both store
  and recall, rather than passage-index/query-search. With asymmetric embedding an identical
  question scored 0.68, below the 0.85 threshold. With symmetric embedding, identical and reworded
  questions score 0.94–1.00 and unrelated ones 0.13–0.17.
- **Exact per-tenant ranking.** Recall uses a `MATERIALIZED` CTE filtered by `user_id`, then sorts
  by `<=>`. Results are exact within a tenant and never starved by other tenants' rows on
  pgvector 0.6.2.
- **Recall only compares vectors from the active embedding model.** Rows from other models are
  reported as `stale`. A re-embed job is future work.
- **Stage 1 has no LLM tone summary.** It is deterministic metrics only; tone analysis moves to
  the M4 synthesis stage.

### Pre-M3 correction: user-provided models only

Presets carry only `base_url`; the chat model is required user input. A blank embedding model
means local `HashingEmbedding1024`. The probe script and all model defaults were removed.
`POST /api/v1/llm/fetch-models` lists `GET /models` for the user's key so they can pick a model.
See `CLAUDE.md` §4.

### M3: Discovery, Deep Scraping & Eligibility (complete)

**Pipeline:** campaign → CRAWLING → EXTRACTING → EVALUATING → QUEUED → COMPLETED. Every
transition, page, opportunity, verdict and queue insert is committed with a `pipeline_events` row
and streamed over SSE.

**Runners:** in-process inside the API by default (dev, `memory://` Redis). With
`CAMPAIGN_RUNNER=arq`, the API enqueues jobs and `uv run worker --queue discovery` runs them.

**Sources:**
- **Websites:** Playwright Chromium with a per-request SSRF route guard, falling back to an httpx
  fetcher that re-validates every redirect hop. Best-first crawl within the site, honouring
  `robots.txt` and per-host politeness.
- **Web search:** Tavily → Brave → zero-key ddgs. Keys come from `TAVILY_API_KEY` /
  `BRAVE_API_KEY`.
- **LinkedIn:** guest listing cards only, with jitter and a page cap.

**Security:**
- The sanitizer strips hidden and scripted DOM and invisible characters, applies NFKC, and flags
  injection attempts.
- Untrusted text goes inside `<untrusted_document>`, and the model must return Pydantic-validated
  JSON.
- The LLM only ever receives the non-PII profile snapshot.

**Eligibility:**
- Stage 1 is deterministic: deadline (passed → EXPIRED), degree, GPA normalized to /4,
  nationality and residency with aliases, graduation window.
- Stage 2 runs on the user's model, only for `other_criteria` that are still undecided.
- Eligible opportunities become `applications` in state QUEUED via the state machine, ranked by
  `deadline_at ASC NULLS LAST`.

**Dedupe:**
- Pages dedupe on content SHA within a campaign.
- Opportunities dedupe on the unique `(user_id, dedupe_key)` and on embedding cosine > 0.92
  (exact, per tenant).

**Findings fixed during M3 (both covered by tests):**
- **Chromium redirect bypass.** Chromium follows HTTP redirects without re-entering
  `context.route()`. The route handler now fetches with `max_redirects=0` and validates each
  `Location` before fulfilling.
- **IP-literal hosts.** The SSRF guard now judges IP-literal hosts directly instead of trusting
  DNS.

### Part 1: Flight recorder and cross-campaign self-healing (complete)

**Trace:**
- Every page stores `crawled_pages.trace`, with the stages DISCOVERY, MEMORY, PREFLIGHT_AND_SSRF,
  BROWSER_FETCH/HTTP_FETCH, DISTILL_AND_SANITIZE and LLM_EXTRACTION.
- Each page also stores `error_stage`, `error_code` and `error_detail`, plus its
  `discovery_source` and `discovery_query`.
- `FileCampaignLog` mirrors everything (redacted) to `.logs/campaigns/campaign_<id>.jsonl` and a
  human-readable `.log`.
- The Campaigns page opens a **Crawl Inspector** drawer per page. It shows the timeline, HTTP
  status, failure stage, Markdown preview, and a "Retry Page with Adaptive Strategy" button.

**Self-healing:** each `domain_playbooks` row (RLS per user and domain) remembers what failed.
- **Fetch:** 403s, empty renders and timeouts escalate `FetchStrategy`: longer hydration, a
  `load` / `networkidle` wait, and a browser/HTTP switch.
- **Extraction:** truncated or invalid JSON escalates `ExtractionStrategy`. Pages over 6000 chars
  are split into overlapping windows, `max_tokens` goes up to 8192, and partial JSON is recovered.
- **Dead URLs:** 404, 410 and login walls are skipped on later campaigns with "Skipped based on
  Campaign #X failure memory".
- **Interrupted campaigns:** a RUNNING campaign left behind by a stopped API is marked FAILED at
  startup.

**Egress proxy:** Chromium traffic goes through an in-process SSRF proxy, so sites see a real
browser TLS fingerprint (`route.fetch` caused 403s).
- Plain-HTTP forwarding is one request per connection (`Connection: close`).
- Otherwise a kept-alive connection would carry the browser's next request to the pinned
  upstream without validation. This was found by the fake-portal E2E test.

### M4: Essay synthesis, form filler, Action Inbox, notifications (complete)

**Essays:**
- Retrieval uses top-k `essay_chunks`, profile facts and the fingerprint.
- Drafts are generated on the user's model under a style spec.
- A critic pass handles banned clichés and unsupported claims, with one revision.
- Each draft gets a **Voice Match** score (feature cosine) and a **Burstiness** score
  `1 - |CV_d - CV_s| / CV_s`.
- When `ai_policy=PROHIBITED`, the draft is flagged `requires_manual`.

**Filler** (`infrastructure/browser/form_filler.py`):
- Runs Playwright behind the egress proxy.
- Fields resolve in order: profile synonyms → documents (decrypted to a temp dir) → essay
  detector → semantic memory.
- A field is filled only at confidence ≥ 0.85, and the filler **never clicks Submit**.
- Below that threshold, the step is checkpointed (encrypted `storage_state`, DOM fingerprint,
  fill ledger, screenshot). A `pending_questions` row is created, the application moves to
  `PAUSED_NEEDS_USER_INPUT`, and the user is notified.
- Answering in the Inbox stores the field, learns the answer into `knowledge_memory_qa`, and moves
  the application to `RESUME_QUEUED`.
- On resume, the checkpoint is restored if its fingerprint matches. Otherwise the ledger is
  replayed from step 1 (for example after the portal dropped the draft).

**Notifications:**
- `INotifier` adapters: SMTP, Resend, Twilio WhatsApp, Meta WhatsApp, and a dev-log fallback.
- Channels are verified with a 6-digit code (stored as a sha256 hash, 15-minute TTL).
- Messages are deduplicated on `template + application_id + state`.
- Webhooks at `/api/v1/webhooks/twilio` and `/api/v1/webhooks/meta` are signature-verified.

**UI:**
- `/inbox`: Questions, Ready for OK, Needs a human, plus a live sidebar badge.
- `/applications/[id]`: timeline, fields with source tags, drafts with scores, checkpoint
  screenshots.
- `/vault/essays` gains a synthesizer.
- `/settings/notifications`.

**Gate:** `tests/integration/test_application_filler.py` runs against the 3-step
`tests/fixtures/fake_portal`. It covers pause → Inbox answer → memory → resume with ledger replay
after a wipe → stop before Submit → a second portal auto-answered from memory.

### Discovery v2: audit fixes and the streaming hub harvester (complete)

**Pipeline** (`application/discovery/runner.py`, `CampaignRun`): producer (sources, then hub
detail links) → bounded `asyncio.Queue` → 2–4 consumers (extract → eligibility → queue).
- Verdicts and queued applications stream *during* the crawl.
- Campaign phases are `CRAWLING` (everything runs), `EXTRACTING` (queue draining) and
  `COMPLETED`.
- One failing page, window or source never aborts the run.
- Cancellation: a watcher polls the status every 0.5 s and cancels the run task, with a
  checkpoint before and after every fetch and every extraction window. It takes effect in < 2 s.

**Listing pages:** `infrastructure/scraping/segmenter.py` finds repeated card / table structures
and keeps `[Title](URL) | Deadline | snippet` together. trafilatura stays for prose.
- **LISTING hubs** with ≥ 3 followable cards are harvested deterministically (`HUB_HARVEST` trace
  step). Their top detail links are fetched, including off-site official pages behind
  listicles, and the LLM runs on those detail pages. Links are scored by `detail_link_score`,
  which penalizes `/page/N>2`, taxonomy and social links.
- **MIXED** pages (prose plus cards) are extracted *and* harvested.

**Extraction:**
- Compact schema.
- No item caps; a visible `truncated_windows` warning beyond the window budget.
- Per-window error isolation.
- Fuzzy title/organization merging that works without an embedding model.
- `llm_timeout_seconds = 180` with 1 retry.
- An optional user-typed `extraction_model` (blank means the chat model is used).

**Failure memory:**
- `domain_playbooks` are saved immediately, in sorted domain order (deadlock-free), including
  escalated strategies after failures.
- A timeout falls back to plain HTTP within the same attempt.
- Error codes are precise: `dns_error`, `tunnel_connect_error`, `bot_challenge` and
  `ssrf_blocked` are distinct.

**Bot challenges:**
- A challenged page gets `BLOCKED_BOT_CHALLENGE` and is remembered per domain; later campaigns
  skip the domain.
- The user can pass the challenge once in a visible browser (Settings › Search & Sessions, or
  the Crawl Inspector). Its encrypted `storage_state` is then used for that domain.
- Automation never solves challenges.

**Data and keys:**
- Failed and empty pages are never dropped: the content-hash uniqueness is a partial index over
  DISTILLED/EXTRACTED pages only.
- Per-user Tavily/Brave keys are encrypted (`search_credentials`).
- `EgressProxy.stop()` tracks and aborts every tunnel; shutdown is bounded at about 1.5 s.

### M5: Final verification gate, submission, LinkedIn auth, hardening (complete)

**Gate** (`application/applications/submission.py`, `domain/applications/verification.py`):
- `preview_hash = sha256(canonical (step, field, value))`.
- `POST /applications/{id}/approve {preview_hash, edits}` does the following in order:
  1. rejects a stale hash with 409;
  2. applies the edits;
  3. records a `final_verifications` row bound to the approved hash;
  4. moves the application to `SUBMIT_APPROVED`.
- `RunSubmission` replays every step with the approved values and reads each control back from
  the page. It clicks Submit only if the observed hash equals the approved hash; any mismatch
  re-stages the application to `AWAITING_FINAL_VERIFICATION`.
- Receipts: a full-page screenshot and the confirmation text are stored encrypted.
- Idempotent: a second approval or run never submits twice. Failures go to `SUBMISSION_FAILED`
  and are never retried automatically.
- Reject moves the application to `CANCELLED`.

**Maintenance:**
- `ReapLeases`: a dead worker's fill run becomes `BLOCKED_NEEDS_HUMAN`; an interrupted submit
  becomes `SUBMISSION_FAILED` with a "check the portal" note.
- `SweepDeadlines` expires past-deadline applications, except while a submit is in flight.
- These run in the API lifespan (in-process mode) and as arq cron jobs on the `system` queue.

**LinkedIn (opt-in):**
- Requires ToS acknowledgement and a headful sign-in.
- The session is encrypted, with a per-day action cap (`browser_sessions.daily_action_cap`).
- `LinkedInAuthenticatedAdapter` is read-only.

**Hardening:**
- API: CSP `default-src 'none'`, frame/sniff/permissions/CORP headers, and HSTS when cookies are
  secure.
- Console: CSP and security headers.
- `backend/Dockerfile.api`, `backend/Dockerfile.worker` (Playwright Chromium) and
  `frontend/Dockerfile` (Next standalone).

**UI:**
- Verification Drawer (Inbox › Ready for OK, and the application inspector): a zoomable
  screenshot, editable fields with source tags and diff highlights, the preview hash, **OK —
  Verify & Submit**, **Reject / Cancel**, and the receipt.

### M6: In-page RAG, RPM pacing, document parsing, evidence RAG, batch staging

- **In-page RAG** (`domain/discovery/rag_filter.py`): a long distilled page is split into
  semantic sections and scored:
  - Stage A: BM25 + opportunity signals + embedding cosine.
  - Stage B (optional): the user's `rerank_model` (`/ranking`).
  The lead section and the relevant sections are kept. When the result fits in 6000 chars it is
  extracted in a single LLM call. Trace step: `RAG_FILTER` (`chars_saved_pct`), shown in the
  Crawl Inspector.
- **Pacing:** one sliding-window limiter per provider host + key (`rpm_limit`, default 36),
  acquired before every attempt, including retries.
- **Documents:**
  - PDF opportunity URLs go through `PdfLayoutParser` (pypdf; wide-gap rows become pipe tables).
  - Sparse or image-only pages and PDFs go to the vision model only if the user set
    `vision_model`.
- **Evidence RAG:** uploaded documents are chunked into `document_chunks` (vector(1024), RLS).
  - Before a required custom question pauses, `ProfileEvidenceRetriever` searches the
    documents, essays and memory.
  - The snippets are PII-redacted before the model sees them.
  - The answer is auto-filled only at combined confidence ≥ 0.85 (`DOCUMENT`/`ESSAY`/`MEMORY`),
    with the snippet stored as field `evidence`.
- **Batch auto-stage:** `POST /applications/batch-prepare`, the Pipeline queue button, or the
  campaign `auto_stage` option. It fills the top-N QUEUED applications by deadline up to the
  final OK gate, with bounded parallelism (`MAX_PARALLEL_FILLS`). It never submits.
