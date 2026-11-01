"""Ports for discovery, extraction, eligibility, and the application queue."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.domain.applications.models import Application, Transition
from app.domain.discovery.listing import HarvestedCard, OutboundLink, PageKind
from app.domain.discovery.models import (
    Campaign,
    CampaignPhase,
    CampaignSettings,
    CrawledPage,
    FetchedPage,
    Opportunity,
    OpportunityExtract,
)
from app.domain.discovery.playbook import DomainPlaybook, ExtractionStrategy, FetchStrategy
from app.domain.discovery.sessions import BrowserSession, SearchCredential
from app.domain.eligibility.rules import RuleResult
from app.domain.ports.hitl import HitlSession
from app.domain.ports.llm import ILLMProvider
from app.domain.shared.enums import (
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
)

# ---- acquisition --------------------------------------------------------------------------------


class ISiteCrawler(Protocol):
    def crawl(
        self,
        seed_url: str,
        keywords: Sequence[str],
        settings: CampaignSettings,
        strategy: FetchStrategy | None = None,
    ) -> AsyncIterator[FetchedPage]:
        """Best-first crawl of one site (same registrable domain), politely rate-limited."""
        ...

    async def fetch(self, url: str, strategy: FetchStrategy | None = None) -> FetchedPage: ...

    async def fetch_document(self, url: str) -> FetchedDocument:
        """Binary download (PDF brochures / calls), SSRF-guarded, size-capped."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    url: str
    final_url: str
    status: int | None
    content_type: str
    data: bytes
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    markdown: str
    pages: int
    method: str  # pypdf | vision


class IDocumentLayoutParser(Protocol):
    async def parse_pdf(self, data: bytes) -> ParsedDocument:
        """Text + tables of a PDF as Markdown (raises on an unreadable file)."""
        ...


class ISiteCrawlerFactory(Protocol):
    def create(
        self, sessions: dict[str, str] | None = None, *, screenshots: bool = False
    ) -> ISiteCrawler:
        """`sessions`: domain -> storage_state JSON of a user-solved browser session.
        `screenshots`: capture text-sparse pages (only when the user configured a vision model)."""
        ...


@dataclass(frozen=True, slots=True)
class ProcessedPage:
    title: str | None
    markdown: str
    content_sha256: str
    injection_flags: list[dict[str, str]]
    kind: str = PageKind.ARTICLE  # article | listing | mixed
    cards: list[HarvestedCard] = field(default_factory=list)  # listing cards, links absolute
    diagnostics: dict[str, Any] = field(default_factory=dict)  # segmenter/distiller stats
    # Off-site official / application links found on the page (best first).
    outbound_links: list[OutboundLink] = field(default_factory=list)


class IContentProcessor(Protocol):
    def process(self, html: str, url: str) -> ProcessedPage:
        """Sanitize (strip hidden/scripted content, flag injection) and distill to Markdown."""
        ...


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str


class ISearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


@dataclass(frozen=True, slots=True)
class Listing:
    title: str
    organization: str | None
    location: str | None
    url: str
    posted: str | None
    source: str


class IListingSource(Protocol):
    async def search(self, keyword: str, location: str | None, limit: int) -> list[Listing]: ...


class IAuthenticatedListingSource(Protocol):
    async def search(
        self, keyword: str, location: str | None, limit: int, storage_state: str
    ) -> list[Listing]:
        """Read-only search with the USER's own signed-in session (opt-in, capped per day)."""
        ...


# ---- reasoning ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    extracts: list[OpportunityExtract]
    diagnostics: dict[str, Any]


class IOpportunityExtractor(Protocol):
    async def extract(
        self,
        llm: ILLMProvider,
        markdown: str,
        source_url: str,
        strategy: ExtractionStrategy | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> ExtractionResult:
        """Raises an error carrying `diagnostics` only when NO window could be extracted (one
        failed window never discards the others). `checkpoint` runs before every window and may
        raise to cancel."""
        ...


class ICampaignLog(Protocol):
    """Flight recorder on disk: `campaign_{id}.jsonl` (machine) + `campaign_{id}.log` (human)."""

    def write(self, campaign_id: uuid.UUID, record: dict[str, Any]) -> None: ...

    def path_for(self, campaign_id: uuid.UUID) -> str: ...


class IPlaybookRepository(Protocol):
    async def all_for_user(self, user_id: uuid.UUID) -> dict[str, DomainPlaybook]: ...

    async def save(self, playbook: DomainPlaybook) -> None: ...


class ISearchFactory(Protocol):
    def create(self, *, tavily_key: str | None, brave_key: str | None) -> ISearchProvider:
        """A search chain with the user's own keys first, then the zero-key fallback."""
        ...


class IBrowserSessionRepository(Protocol):
    async def list(self, user_id: uuid.UUID) -> list[BrowserSession]: ...
    async def get(self, user_id: uuid.UUID, session_id: uuid.UUID) -> BrowserSession | None: ...
    async def find(self, user_id: uuid.UUID, domain: str, kind: str) -> BrowserSession | None: ...
    async def upsert(self, session: BrowserSession) -> None:
        """Insert or replace the session for (user, domain, kind)."""
        ...

    async def update_usage(self, session: BrowserSession) -> None: ...
    async def delete(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool: ...


class ISearchCredentialRepository(Protocol):
    async def list(self, user_id: uuid.UUID) -> list[SearchCredential]: ...
    async def upsert(self, credential: SearchCredential) -> None: ...
    async def delete(self, user_id: uuid.UUID, provider: str) -> bool: ...


class IHeadfulSessionCapturer(Protocol):
    async def capture(
        self,
        url: str,
        *,
        timeout_s: float,
        wait_for_close: bool = False,
        hitl: HitlSession | None = None,
    ) -> str:
        """Open a VISIBLE browser at `url` (or, with `hitl`, a remote one streamed into the
        console) for the user to pass a challenge / log in; return the resulting storage_state
        JSON once the page is usable (or raise on timeout/close)."""
        ...


class IFuzzyEligibilityEvaluator(Protocol):
    async def evaluate(
        self, llm: ILLMProvider, criteria: Sequence[str], profile: dict[str, Any]
    ) -> list[RuleResult]: ...


class ICampaignRunner(Protocol):
    async def start(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None: ...


# ---- repositories -------------------------------------------------------------------------------


class ICampaignRepository(Protocol):
    async def add(self, campaign: Campaign) -> None: ...

    async def get(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign | None: ...

    async def list(self, user_id: uuid.UUID, limit: int) -> list[Campaign]: ...

    async def update_state(
        self,
        user_id: uuid.UUID,
        campaign_id: uuid.UUID,
        *,
        status: CampaignStatus | None = None,
        phase: CampaignPhase | None = None,
        counters: dict[str, int] | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None: ...

    async def status_of(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID
    ) -> CampaignStatus | None: ...

    async def active_for_user(self, user_id: uuid.UUID) -> Campaign | None:
        """The user's DRAFT/RUNNING campaign, if any (one active campaign per user)."""
        ...

    async def claim_lease(
        self,
        user_id: uuid.UUID,
        campaign_id: uuid.UUID,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        """Take (or renew) the run lease unless another live owner holds it."""
        ...

    async def release_lease(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID, owner: str
    ) -> None: ...

    async def stale_running(self, now: datetime, limit: int = 100) -> Sequence[Campaign]:
        """System scope: RUNNING campaigns whose lease expired (their worker died)."""
        ...

    async def mark_resumed(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID, pending_until: datetime
    ) -> None:
        """Count one automatic resume; the run waits under the pending owner until claimed."""
        ...


class ICrawledPageRepository(Protocol):
    async def add(self, page: CrawledPage) -> bool:
        """False when the same content was already stored for this campaign."""
        ...

    async def for_campaign(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID, statuses: Sequence[CrawlStatus] | None
    ) -> list[CrawledPage]: ...

    async def set_status(
        self, user_id: uuid.UUID, page_id: uuid.UUID, status: CrawlStatus, error: str | None = None
    ) -> None: ...

    async def get(self, user_id: uuid.UUID, page_id: uuid.UUID) -> CrawledPage | None: ...

    async def update_diagnostics(self, page: CrawledPage) -> None:
        """Persist status, trace, error_* and strategy after extraction or a retry."""
        ...


@dataclass(frozen=True, slots=True)
class OpportunityFilter:
    outcome: EligibilityOutcome | None = None
    campaign_id: uuid.UUID | None = None
    search: str | None = None
    limit: int = 200


class IOpportunityRepository(Protocol):
    async def add(self, opportunity: Opportunity, vector: Sequence[float]) -> bool:
        """False on (user_id, dedupe_key) conflict."""
        ...

    async def most_similar(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str
    ) -> tuple[uuid.UUID, float] | None: ...

    async def get(self, user_id: uuid.UUID, opportunity_id: uuid.UUID) -> Opportunity | None: ...

    async def search(self, user_id: uuid.UUID, flt: OpportunityFilter) -> list[Opportunity]: ...

    async def for_campaign(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID
    ) -> list[Opportunity]: ...

    async def set_outcome(
        self, user_id: uuid.UUID, opportunity_id: uuid.UUID, outcome: EligibilityOutcome
    ) -> None: ...

    async def set_portal_url(
        self, user_id: uuid.UUID, opportunity_id: uuid.UUID, portal_url: str
    ) -> None: ...

    async def counts(self, user_id: uuid.UUID) -> dict[str, int]: ...

    async def recent_titles(
        self, user_id: uuid.UUID, limit: int
    ) -> list[tuple[uuid.UUID, str, str | None, str | None]]:
        """(id, title, organization, portal_url) of the user's newest opportunities."""
        ...


@dataclass(frozen=True, slots=True)
class Verdict:
    id: uuid.UUID
    user_id: uuid.UUID
    opportunity_id: uuid.UUID
    outcome: EligibilityOutcome
    reasons: list[RuleResult]
    profile_snapshot_hash: str
    model: str | None
    evaluated_at: datetime


class IVerdictRepository(Protocol):
    async def add(self, verdict: Verdict) -> None: ...

    async def latest(self, user_id: uuid.UUID, opportunity_id: uuid.UUID) -> Verdict | None: ...


class IApplicationRepository(Protocol):
    async def add(self, application: Application) -> bool:
        """False if an application for this opportunity already exists."""
        ...

    async def add_transition(self, user_id: uuid.UUID, transition: Transition) -> None: ...

    async def rerank_queue(self, user_id: uuid.UUID) -> None:
        """priority_rank = 1..n over QUEUED apps by deadline ASC NULLS LAST."""
        ...

    async def queue(
        self, user_id: uuid.UUID, limit: int
    ) -> list[tuple[Application, Opportunity]]: ...

    async def count_by_state(self, user_id: uuid.UUID) -> dict[ApplicationState, int]: ...
