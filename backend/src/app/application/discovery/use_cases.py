"""Discovery campaigns: create, run (Crawling -> Extracting -> Evaluating -> Queued), query."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.discovery.runner import CampaignRun
from app.application.llm.use_cases import (
    GetEmbeddingProvider,
    ResolveChatProvider,
    ResolveRerankProvider,
    ResolveVisionProvider,
)
from app.domain.applications.models import Application
from app.domain.discovery.models import (
    Campaign,
    CampaignPhase,
    CampaignSettings,
    CampaignSource,
    CrawledPage,
    Opportunity,
    clean_keywords,
    empty_counters,
)
from app.domain.discovery.sessions import SessionKind
from app.domain.events.models import NewEvent
from app.domain.ports.crypto import ICipher
from app.domain.ports.discovery import (
    IAuthenticatedListingSource,
    ICampaignLog,
    IContentProcessor,
    IDocumentLayoutParser,
    IFuzzyEligibilityEvaluator,
    IListingSource,
    IOpportunityExtractor,
    ISearchFactory,
    ISearchProvider,
    ISiteCrawlerFactory,
    OpportunityFilter,
    Verdict,
)
from app.domain.ports.net import IUrlGuard
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import (
    Actor,
    ApplicationState,
    CampaignStatus,
    Scope,
    SourceType,
)
from app.domain.shared.errors import ConflictError, NotFoundError, UnsafeUrlError, ValidationError
from app.domain.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class DiscoveryDeps:
    uow_factory: IUnitOfWorkFactory
    clock: Clock
    url_guard: IUrlGuard
    crawler_factory: ISiteCrawlerFactory
    processor: IContentProcessor
    search: ISearchProvider
    listings: IListingSource
    extractor: IOpportunityExtractor
    fuzzy: IFuzzyEligibilityEvaluator
    chat: ResolveChatProvider
    embeddings: GetEmbeddingProvider
    campaign_log: ICampaignLog
    # Decrypts user-solved browser sessions and per-user search keys (None in some tests).
    cipher: ICipher | None = None
    search_factory: ISearchFactory | None = None
    linkedin_auth: IAuthenticatedListingSource | None = None
    rerank: ResolveRerankProvider | None = None  # user's optional reranker (in-page RAG)
    vision: ResolveVisionProvider | None = None  # user's optional vision model
    layout: IDocumentLayoutParser | None = None  # PDF opportunity documents
    # Batch auto-stage (opt-in per campaign): prepare the top-N queued applications.
    auto_stage: Callable[[uuid.UUID, int], Awaitable[list[uuid.UUID]]] | None = None


@dataclass(frozen=True, slots=True)
class CampaignInput:
    name: str
    scope: Scope
    keywords: list[str]
    target_urls: list[str] = field(default_factory=list)
    use_websites: bool = True
    use_search: bool = True
    use_linkedin: bool = False
    use_linkedin_auth: bool = False  # opt-in: needs a signed-in LinkedIn browser session
    settings: CampaignSettings = field(default_factory=CampaignSettings)


async def _emit(
    deps: DiscoveryDeps,
    user_id: uuid.UUID,
    campaign_id: uuid.UUID,
    type_: str,
    payload: dict[str, Any],
) -> None:
    async with deps.uow_factory(user_id) as uow:
        await uow.events.append(
            NewEvent(user_id=user_id, type=type_, payload=payload, campaign_id=campaign_id)
        )
        await uow.commit()


# ---- create / query -----------------------------------------------------------------------------


@dataclass(slots=True)
class CreateCampaign:
    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID, data: CampaignInput) -> Campaign:
        keywords = clean_keywords(data.keywords)
        data.settings.validate()
        await self.deps.chat.execute(user_id)  # fail fast: an LLM model must be configured
        async with self.deps.uow_factory(user_id) as uow:
            active = await uow.campaigns.active_for_user(user_id)
        if active is not None:
            # One running campaign per user: concurrent runs competed for the same domains
            # (and the same failure-memory rows) and slowed each other down.
            raise ConflictError(
                f"campaign '{active.name}' is still running: cancel it or wait for it to finish"
            )
        urls: list[str] = []
        if data.use_websites:
            for raw in data.target_urls:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    urls.append(await self.deps.url_guard.validate(raw))
                except UnsafeUrlError as exc:
                    raise ValidationError(f"{raw}: {exc.message}") from exc
            urls = list(dict.fromkeys(urls))
        if data.use_websites and not urls and not (data.use_search or data.use_linkedin):
            raise ValidationError("add at least one target URL or enable another source")
        if not (urls or data.use_search or data.use_linkedin or data.use_linkedin_auth):
            raise ValidationError("enable at least one source")

        now = self.deps.clock.now()
        campaign_id = new_uuid7()
        sources = [
            CampaignSource(new_uuid7(), user_id, campaign_id, SourceType.WEBSITE, url)
            for url in urls
        ]
        if data.use_search:
            sources.append(
                CampaignSource(new_uuid7(), user_id, campaign_id, SourceType.WEB_SEARCH, None)
            )
        if data.use_linkedin:
            sources.append(
                CampaignSource(new_uuid7(), user_id, campaign_id, SourceType.LINKEDIN_PUBLIC, None)
            )
        if data.use_linkedin_auth:
            async with self.deps.uow_factory(user_id) as uow:
                session = await uow.browser_sessions.find(
                    user_id, "linkedin.com", SessionKind.LINKEDIN
                )
            if session is None or session.tos_acknowledged_at is None:
                raise ValidationError(
                    "connect LinkedIn first (Settings > Search & Sessions): it is opt-in and "
                    "needs your acknowledgement of LinkedIn's User Agreement"
                )
            sources.append(
                CampaignSource(new_uuid7(), user_id, campaign_id, SourceType.LINKEDIN_AUTH, None)
            )
        campaign = Campaign(
            id=campaign_id,
            user_id=user_id,
            name=(data.name or "").strip()[:120] or ", ".join(keywords)[:120],
            scope=data.scope,
            keywords=keywords,
            status=CampaignStatus.DRAFT,
            phase=CampaignPhase.PENDING,
            settings=data.settings,
            counters=empty_counters(),
            error=None,
            created_at=now,
            sources=sources,
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.campaigns.add(campaign)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="campaign.created",
                entity="campaigns",
                entity_id=campaign_id,
                metadata={"sources": len(sources)},
            )
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="campaign.created",
                    campaign_id=campaign_id,
                    payload={"campaign_id": str(campaign_id), "name": campaign.name},
                )
            )
            await uow.commit()
        return campaign


@dataclass(slots=True)
class ListCampaigns:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID, limit: int = 50) -> list[Campaign]:
        async with self.uow_factory(user_id) as uow:
            return await uow.campaigns.list(user_id, limit)


@dataclass(frozen=True, slots=True)
class CampaignDetail:
    campaign: Campaign
    pages: list[CrawledPage]


@dataclass(slots=True)
class GetCampaign:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> CampaignDetail:
        async with self.uow_factory(user_id) as uow:
            campaign = await uow.campaigns.get(user_id, campaign_id)
            if campaign is None:
                raise NotFoundError("campaign not found")
            pages = await uow.pages.for_campaign(user_id, campaign_id, None)
        return CampaignDetail(campaign=campaign, pages=pages)


@dataclass(slots=True)
class GetPage:
    """Full diagnostic record of one crawled page (trace, errors, Markdown) for the inspector."""

    uow_factory: IUnitOfWorkFactory

    async def execute(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID, page_id: uuid.UUID
    ) -> CrawledPage:
        async with self.uow_factory(user_id) as uow:
            page = await uow.pages.get(user_id, page_id)
        if page is None or page.campaign_id != campaign_id:
            raise NotFoundError("page not found")
        return page


@dataclass(slots=True)
class CancelCampaign:
    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            campaign = await uow.campaigns.get(user_id, campaign_id)
            if campaign is None:
                raise NotFoundError("campaign not found")
            if not campaign.is_active:
                return
            await uow.campaigns.update_state(
                user_id,
                campaign_id,
                status=CampaignStatus.CANCELLED,
                phase=CampaignPhase.CANCELLED,
                finished_at=self.deps.clock.now(),
            )
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="campaign.phase",
                    campaign_id=campaign_id,
                    payload={
                        "campaign_id": str(campaign_id),
                        "phase": "CANCELLED",
                        "status": "CANCELLED",
                    },
                )
            )
            await uow.commit()


@dataclass(slots=True)
class ListOpportunities:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID, flt: OpportunityFilter) -> list[Opportunity]:
        async with self.uow_factory(user_id) as uow:
            return await uow.opportunities.search(user_id, flt)


@dataclass(slots=True)
class GetOpportunity:
    uow_factory: IUnitOfWorkFactory

    async def execute(
        self, user_id: uuid.UUID, opportunity_id: uuid.UUID
    ) -> tuple[Opportunity, Verdict | None]:
        async with self.uow_factory(user_id) as uow:
            opp = await uow.opportunities.get(user_id, opportunity_id)
            if opp is None:
                raise NotFoundError("opportunity not found")
            return opp, await uow.verdicts.latest(user_id, opportunity_id)


@dataclass(frozen=True, slots=True)
class PipelineSummary:
    websites_crawled: int
    opportunities_found: int
    eligible: int
    needs_input: int
    ready_for_ok: int
    submitted: int
    queued: int
    latest_campaign: Campaign | None


@dataclass(slots=True)
class GetPipelineSummary:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> PipelineSummary:
        async with self.uow_factory(user_id) as uow:
            counts = await uow.opportunities.counts(user_id)
            states = await uow.applications.count_by_state(user_id)
            latest = await uow.campaigns.list(user_id, 1)
        return PipelineSummary(
            websites_crawled=counts.get("pages_crawled", 0),
            opportunities_found=counts.get("opportunities", 0),
            eligible=counts.get("eligible", 0),
            needs_input=states.get(ApplicationState.PAUSED_NEEDS_USER_INPUT, 0),
            ready_for_ok=states.get(ApplicationState.AWAITING_FINAL_VERIFICATION, 0),
            submitted=states.get(ApplicationState.SUBMITTED, 0),
            queued=states.get(ApplicationState.QUEUED, 0),
            latest_campaign=latest[0] if latest else None,
        )


@dataclass(slots=True)
class ListApplicationQueue:
    uow_factory: IUnitOfWorkFactory

    async def execute(
        self, user_id: uuid.UUID, limit: int = 100
    ) -> list[tuple[Application, Opportunity]]:
        async with self.uow_factory(user_id) as uow:
            return await uow.applications.queue(user_id, limit)


# ---- the run ------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunCampaign:
    """Executes one campaign end to end. Every phase transition, page, opportunity and verdict
    is committed with a `pipeline_events` row (streamed over SSE) and written to the on-disk
    flight recorder."""

    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            campaign = await uow.campaigns.get(user_id, campaign_id)
        if campaign is None or campaign.status is not CampaignStatus.DRAFT:
            return
        await CampaignRun(self.deps, campaign).execute()


@dataclass(slots=True)
class RetryPage:
    """Re-run one page (fetch -> distill -> extract -> evaluate -> queue) with an ESCALATED
    strategy chosen from its last failure and the domain playbook."""

    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID, campaign_id: uuid.UUID, page_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            campaign = await uow.campaigns.get(user_id, campaign_id)
            page = await uow.pages.get(user_id, page_id)
        if campaign is None or page is None or page.campaign_id != campaign_id:
            raise NotFoundError("page not found")
        await CampaignRun(self.deps, campaign).retry_page(page)


@dataclass(slots=True)
class SweepInterruptedCampaigns:
    """At startup (in-process runner): campaigns still RUNNING were interrupted by a restart."""

    system_uow_factory: IUnitOfWorkFactory
    clock: Clock

    async def execute(self) -> int:
        async with self.system_uow_factory() as uow:
            count = await uow.campaigns.fail_running(
                "interrupted: the app restarted while this campaign was running. Open the "
                "campaign and use Retry on any page, or launch it again.",
                self.clock.now(),
            )
            await uow.commit()
        return count
