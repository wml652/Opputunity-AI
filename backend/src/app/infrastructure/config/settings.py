from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    environment: Literal["development", "test", "production"] = "development"
    # Local runtime only: opens a VISIBLE browser so the user can pass a bot challenge or sign
    # in to LinkedIn themselves (cookies are then stored encrypted). Never in production.
    allow_headful_capture: bool = True
    # Lease reaper + deadline sweep cadence (in-process runner).
    maintenance_interval_seconds: float = 300.0
    # Browser filling runs in parallel (batch auto-stage); each is a Chromium instance.
    max_parallel_fills: int = 2
    # Global browser pool (Redis, across every worker process): automated browsers (fills,
    # submissions, crawlers) and remote human sessions (Teach Mode, sign-in, challenges).
    max_browsers: int = 3
    max_hitl_browsers: int = 2
    # Human-in-the-loop browsers: "local" = a visible window on this machine (dev), "remote" =
    # headless Chromium streamed into the console (servers), "auto" = remote in production.
    hitl_mode: Literal["auto", "local", "remote"] = "auto"
    # Memory admission: a fill waits (up to 2 min) for this much free RAM before its browser.
    min_free_memory_mb: int = 1200
    # Extra aggregator / directory hosts (beyond the built-in list): never typed into by the
    # form filler; their URLs are replaced by the official outbound link.
    aggregator_domains: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    log_json: bool = False

    # Database: the app role must NOT be a superuser (superusers bypass RLS).
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/opportunity"
    database_migration_url: str | None = None  # owner role for Alembic; defaults to database_url
    db_pool_size: int = 10

    redis_url: str = "redis://localhost:6379/0"

    # Security
    jwt_secret: SecretStr = SecretStr("change-me-dev-only-secret-at-least-32-bytes!!")
    jwt_issuer: str = "opportunity-platform"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30
    # "<version>:<base64 32-byte key>" comma-separated; highest version encrypts.
    encryption_keys: SecretStr = SecretStr("")
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 64 * 1024
    argon2_parallelism: int = 2

    cookie_secure: bool = False
    cookie_domain: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Outbound / LLM
    # Reasoning models (e.g. long-thinking NIM models) need minutes for one extraction window.
    llm_timeout_seconds: float = 180.0
    # Retries per chat call on timeout/429/5xx. Kept low: wall-clock = timeout x (retries + 1).
    llm_max_retries: int = 1
    llm_allow_private_hosts: bool = False  # allow custom base URLs on localhost/LAN (self-hosted)
    llm_allow_insecure_http: bool = False

    # Discovery / scraping
    # "inprocess": campaigns and fills run inside the API (dev, memory:// Redis). "arq": the
    # API enqueues jobs; `uv run worker --queue discovery|browser|system` executes them.
    execution_mode: Literal["inprocess", "arq"] = Field(
        "inprocess",
        validation_alias=AliasChoices("execution_mode", "campaign_runner"),
    )
    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None
    crawl_use_playwright: bool = True  # falls back to plain HTTP if Chromium is not installed
    crawl_politeness_seconds: float = 1.0
    crawl_respect_robots: bool = True
    # Local fixture portals ("127.0.0.1:PORT") that bypass the SSRF guard. TEST MODE ONLY.
    scraper_test_fixture_hosts: list[str] = Field(default_factory=list)

    # Campaign flight-recorder logs (<logs_dir>/campaigns/campaign_<id>.jsonl|.log)
    logs_dir: str = "../.logs"
    # Public URL of the console, used in notification links
    console_url: str = "http://localhost:3000"

    # Notifications (all optional). Without a provider, channels fall back to the dev log
    # notifier (writes <logs_dir>/notifications.log, NOT delivered) unless disabled.
    notifications_dev_log: bool = True
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str = "OPPORTUNITIES ON THE GO <noreply@localhost>"
    smtp_starttls: bool = True
    resend_api_key: SecretStr | None = None
    resend_sender: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_whatsapp_from: str | None = None
    meta_wa_access_token: SecretStr | None = None
    meta_wa_phone_number_id: str | None = None
    meta_wa_template: str = "opportunity_update"
    meta_app_secret: SecretStr | None = None
    meta_verify_token: SecretStr | None = None
    public_api_url: str = "http://127.0.0.1:8000"  # used to verify Twilio webhook signatures

    # Encrypted document blobs (local disk in dev; swap the adapter for S3 in the cloud)
    storage_dir: str = ".storage"

    # Workers / events
    # DEV ONLY: skip login; every request acts as a local dev user. Refused in production.
    dev_auth_bypass: bool = False
    outbox_relay_in_api: bool = True  # dev convenience; production runs the relay in workers
    outbox_poll_interval_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    worker_heartbeat_ttl_seconds: int = 30

    @field_validator("database_url", "database_migration_url")
    @classmethod
    def _asyncpg_driver(cls, value: str | None) -> str | None:
        if value is None:
            return None
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_for_runtime(self) -> None:
        if not self.encryption_keys.get_secret_value():
            raise RuntimeError(
                "ENCRYPTION_KEYS is not set. Generate one with: uv run python -m "
                "app.infrastructure.crypto.keygen"
            )
        if self.is_production:
            if self.dev_auth_bypass:
                raise RuntimeError("DEV_AUTH_BYPASS must never be enabled in production")
            if self.redis_url.startswith("memory://"):
                raise RuntimeError("REDIS_URL=memory:// is a single-process dev mode only")
            if len(self.jwt_secret.get_secret_value()) < 32 or "change-me" in (
                self.jwt_secret.get_secret_value()
            ):
                raise RuntimeError("JWT_SECRET must be a strong random value in production")
            if not self.cookie_secure:
                raise RuntimeError("COOKIE_SECURE must be true in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
