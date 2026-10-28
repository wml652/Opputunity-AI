"""Ports for discovery, extraction, eligibility, and the application queue."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.domain.applications.models import Application, Transition
from app.domain.discovery.models import (
    Campaign,
    CampaignPhase,
    CampaignSettings,
    CrawledPage,
    FetchedPage,
    Opportunity,
    OpportunityExtract,
)
from app.domain.eligibility.rules import RuleResult
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
        self, seed_url: str, keywords: Sequence[str], settings: CampaignSettings
    ) -> AsyncIterator[FetchedPage]:
        """Breadth-first crawl of one site (same registrable domain), politely rate-limited."""
        ...

    async def fetch(self, url: str) -> FetchedPage: ...

    async def aclose(self) -> None: ...


class ISiteCrawlerFactory(Protocol):
    def create(self) -> ISiteCrawler: ...


@dataclass(frozen=True, slots=True)
class ProcessedPage:
    title: str | None
    markdown: str
    content_sha256: str
    injection_flags: list[dict[str, str]]


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


# ---- reasoning ----------------------------------------------------------------------------------


class IOpportunityExtractor(Protocol):
    async def extract(
        self, llm: ILLMProvider, markdown: str, source_url: str
    ) -> list[OpportunityExtract]: ...


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

    async def counts(self, user_id: uuid.UUID) -> dict[str, int]: ...


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
