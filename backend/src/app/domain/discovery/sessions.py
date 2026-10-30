"""Stored browser sessions and per-user search credentials (pure domain).

A *browser session* is an encrypted Playwright `storage_state` (cookies + localStorage) that the
USER produced in a headful browser: passing a bot challenge for a domain once, or logging in to
LinkedIn for the opt-in authenticated adapter. Automation never solves challenges itself."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from app.domain.llm.provider import EncryptedSecret

# AAD table names: aad(table, column, user_id, row_id) binds each ciphertext to its row.
BROWSER_SESSION_AAD = "browser_sessions"  # column "storage_state"
SEARCH_CREDENTIAL_AAD = "search_credentials"  # column "key"


class SessionKind:
    CHALLENGE = "CHALLENGE"  # user passed a bot challenge (Cloudflare etc.) for a domain
    PORTAL = "PORTAL"  # user logged in to an application portal
    LINKEDIN = "LINKEDIN"  # opt-in authenticated LinkedIn (ToS acknowledged, daily cap)

    ALL = (CHALLENGE, PORTAL, LINKEDIN)


LINKEDIN_DEFAULT_DAILY_CAP = 25
LINKEDIN_MAX_DAILY_CAP = 100


@dataclass(slots=True)
class BrowserSession:
    id: uuid.UUID
    user_id: uuid.UUID
    domain: str
    kind: str
    storage_state: EncryptedSecret
    created_at: datetime
    tos_acknowledged_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    daily_action_cap: int | None = None
    actions_today: int = 0
    actions_day: date | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def actions_used(self, today: date) -> int:
        return self.actions_today if self.actions_day == today else 0

    def can_act(self, today: date) -> bool:
        if self.daily_action_cap is None:
            return True
        return self.actions_used(today) < self.daily_action_cap

    def record_action(self, today: date, now: datetime) -> None:
        self.actions_today = self.actions_used(today) + 1
        self.actions_day = today
        self.last_used_at = now


class SearchProviderName:
    TAVILY = "tavily"
    BRAVE = "brave"

    ALL = (TAVILY, BRAVE)


@dataclass(slots=True)
class SearchCredential:
    id: uuid.UUID
    user_id: uuid.UUID
    provider: str
    key: EncryptedSecret
    key_last4: str
    updated_at: datetime
