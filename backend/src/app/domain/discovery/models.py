"""Discovery campaigns, crawled pages and extracted opportunities (pure domain)."""

from __future__ import annotations

import enum
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

from app.domain.eligibility.rules import EligibilityRules
from app.domain.shared.enums import (
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
    OpportunityType,
    Scope,
    SourceType,
)
from app.domain.shared.errors import ValidationError

MAX_KEYWORDS = 20
MAX_TARGET_URLS = 20


class CampaignPhase(enum.StrEnum):
    PENDING = "PENDING"
    CRAWLING = "CRAWLING"
    EXTRACTING = "EXTRACTING"
    EVALUATING = "EVALUATING"
    QUEUED = "QUEUED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AiPolicy(enum.StrEnum):
    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"


COUNTER_KEYS = (
    "websites_crawled",
    "pages_blocked",
    "pages_failed",
    "opportunities_found",
    "duplicates",
    "eligible",
    "ineligible",
    "expired",
    "uncertain",
    "queued",
    "pages_skipped",
    "pages_retried",
    "adapted",
)


def empty_counters() -> dict[str, int]:
    return dict.fromkeys(COUNTER_KEYS, 0)


@dataclass(frozen=True, slots=True)
class CampaignSettings:
    max_pages_per_site: int = 15
    max_depth: int = 2
    search_results_per_query: int = 5
    max_search_pages: int = 20
    home_country: str | None = None  # used for LOCAL scope queries

    def validate(self) -> None:
        if not 1 <= self.max_pages_per_site <= 100:
            raise ValidationError("max_pages_per_site must be 1-100")
        if not 0 <= self.max_depth <= 4:
            raise ValidationError("max_depth must be 0-4")
        if not 1 <= self.search_results_per_query <= 20:
            raise ValidationError("search_results_per_query must be 1-20")
        if not 1 <= self.max_search_pages <= 100:
            raise ValidationError("max_search_pages must be 1-100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_pages_per_site": self.max_pages_per_site,
            "max_depth": self.max_depth,
            "search_results_per_query": self.search_results_per_query,
            "max_search_pages": self.max_search_pages,
            "home_country": self.home_country,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CampaignSettings:
        known = {k: data[k] for k in CampaignSettings().to_dict() if k in data}
        return CampaignSettings(**known)


@dataclass(slots=True)
class CampaignSource:
    id: uuid.UUID
    user_id: uuid.UUID
    campaign_id: uuid.UUID
    source_type: SourceType
    url: str | None
    enabled: bool = True


@dataclass(slots=True)
class Campaign:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    scope: Scope
    keywords: list[str]
    status: CampaignStatus
    phase: CampaignPhase
    settings: CampaignSettings
    counters: dict[str, int]
    error: str | None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sources: list[CampaignSource] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status in (CampaignStatus.DRAFT, CampaignStatus.RUNNING)


def clean_keywords(raw: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in raw:
        kw = " ".join(item.split())[:100]
        if kw and kw.lower() not in seen:
            seen[kw.lower()] = kw
    if not seen:
        raise ValidationError("at least one keyword is required")
    if len(seen) > MAX_KEYWORDS:
        raise ValidationError(f"at most {MAX_KEYWORDS} keywords")
    return list(seen.values())


def build_search_queries(keywords: list[str], scope: Scope, home_country: str | None) -> list[str]:
    """keywords x scope. LOCAL pins the student's country; INTERNATIONAL looks abroad / remote;
    BOTH runs both variants. Queries ask for open calls with deadlines."""
    queries: list[str] = []
    year = datetime.now(UTC).year
    for kw in keywords:
        if scope in (Scope.LOCAL, Scope.BOTH):
            where = home_country or "local"
            queries.append(f'"{kw}" {where} {year} application deadline')
        if scope in (Scope.INTERNATIONAL, Scope.BOTH):
            queries.append(f'"{kw}" international students {year} apply deadline')
    return list(dict.fromkeys(queries))


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    final_url: str
    status: int | None
    html: str
    blocked: bool = False  # SSRF guard refused the URL (or a redirect hop)
    error: str | None = None
    blocked_requests: int = 0  # subresources/redirects the browser was prevented from loading
    trace: list[dict[str, Any]] = field(default_factory=list)  # flight-recorder steps
    strategy: dict[str, Any] = field(default_factory=dict)  # FetchStrategy actually used


@dataclass(slots=True)
class CrawledPage:
    id: uuid.UUID
    user_id: uuid.UUID
    campaign_id: uuid.UUID
    source_id: uuid.UUID | None
    url: str
    final_url: str | None
    http_status: int | None
    status: CrawlStatus
    title: str | None
    content_sha256: str | None
    markdown: str | None
    injection_flags: list[dict[str, str]]
    error: str | None
    fetched_at: datetime
    trace: list[dict[str, Any]] = field(default_factory=list)
    error_stage: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    discovery_source: str | None = None  # seed | followed_link | web_search | linkedin | retry
    discovery_query: str | None = None
    strategy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OpportunityExtract:
    """What the extractor (LLM) returns for one opportunity on a page."""

    title: str
    organization: str | None
    type: OpportunityType
    location: str | None
    is_international: bool | None
    deadline: date | None
    deadline_raw: str | None
    portal_url: str | None
    summary: str
    eligibility: EligibilityRules
    ai_policy: AiPolicy = AiPolicy.UNKNOWN


@dataclass(slots=True)
class Opportunity:
    id: uuid.UUID
    user_id: uuid.UUID
    campaign_id: uuid.UUID | None
    page_id: uuid.UUID | None
    type: OpportunityType
    title: str
    organization: str | None
    location: str | None
    is_international: bool | None
    deadline_at: datetime | None
    deadline_raw: str | None
    portal_url: str | None
    source_url: str | None
    summary: str
    eligibility: EligibilityRules
    ai_policy: AiPolicy
    dedupe_key: str
    embedding_model: str
    latest_outcome: EligibilityOutcome | None
    created_at: datetime


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "and", "in", "at", "to", "program", "programme"}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    words = [w for w in _NON_ALNUM.split(text) if w and w not in _STOP]
    return " ".join(words)


def dedupe_key(title: str, organization: str | None, deadline: date | None) -> str:
    """Stable identity across sources: normalized organization + title + deadline date."""
    return "|".join(
        [
            normalize_text(organization),
            normalize_text(title),
            deadline.isoformat() if deadline else "",
        ]
    )[:500]


def deadline_to_datetime(deadline: date | None) -> datetime | None:
    """Deadlines without a time are treated as end of day UTC (the most lenient reading)."""
    if deadline is None:
        return None
    return datetime(deadline.year, deadline.month, deadline.day, 23, 59, 59, tzinfo=UTC)


def registrable_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


DEDUPE_SIMILARITY = 0.92
