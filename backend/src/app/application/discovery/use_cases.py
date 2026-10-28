"""Discovery campaigns: create, run (Crawling -> Extracting -> Evaluating -> Queued), query."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.application.llm.use_cases import GetEmbeddingProvider, ResolveChatProvider
from app.domain.applications.models import Application, advance
from app.domain.discovery.models import (
    DEDUPE_SIMILARITY,
    Campaign,
    CampaignPhase,
    CampaignSettings,
    CampaignSource,
    CrawledPage,
    FetchedPage,
    Opportunity,
    OpportunityExtract,
    build_search_queries,
    clean_keywords,
    deadline_to_datetime,
    dedupe_key,
    empty_counters,
    registrable_host,
)
from app.domain.discovery.playbook import (
    PERMANENT_CODES,
    DomainPlaybook,
    FetchStrategy,
    Stage,
    StepStatus,
    Trace,
    escalate_extraction,
    escalate_fetch,
    fetch_error_code,
    normalize_url,
)
from app.domain.eligibility.rules import (
    RuleResult,
    RuleSource,
    RuleStatus,
    decide,
    evaluate_deterministic,
    profile_snapshot,
)
from app.domain.events.models import NewEvent
from app.domain.ports.discovery import (
    ICampaignLog,
    IContentProcessor,
    IFuzzyEligibilityEvaluator,
    IListingSource,
    IOpportunityExtractor,
    ISearchProvider,
    ISiteCrawler,
    ISiteCrawlerFactory,
    OpportunityFilter,
    Verdict,
)
from app.domain.ports.llm import IEmbeddingProvider, ILLMProvider
from app.domain.ports.net import IUrlGuard
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.profile.models import StudentPII, StudentProfile
from app.domain.shared.clock import Clock
from app.domain.shared.enums import (
    Actor,
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
    Scope,
    SourceType,
)
from app.domain.shared.errors import DomainError, NotFoundError, UnsafeUrlError, ValidationError
from app.domain.shared.ids import new_uuid7

MIN_MARKDOWN_CHARS = 120


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


@dataclass(frozen=True, slots=True)
class CampaignInput:
    name: str
    scope: Scope
    keywords: list[str]
    target_urls: list[str] = field(default_factory=list)
    use_websites: bool = True
    use_search: bool = True
    use_linkedin: bool = False
    settings: CampaignSettings = field(default_factory=CampaignSettings)


class _CancelledError(Exception):
    pass


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
        if not (urls or data.use_search or data.use_linkedin):
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
        run = _Run(self.deps, campaign)
        await run.execute()


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
        run = _Run(self.deps, campaign)
        await run.retry_page(page)


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


_LOGIN_WALL = re.compile(
    r"\b(sign in|log in|login|create an account)\b.{0,60}\b(continue|view|access|see)\b",
    re.I | re.S,
)


class _Run:
    def __init__(self, deps: DiscoveryDeps, campaign: Campaign) -> None:
        self.deps = deps
        self.c = campaign
        self.user = campaign.user_id
        self.counters = {**empty_counters(), **campaign.counters}
        self.playbooks: dict[str, DomainPlaybook] = {}
        self.dirty: set[str] = set()
        self.crawler: ISiteCrawler | None = None
        self.label = f"#{str(campaign.id)[-6:]}"

    # -- flight recorder -------------------------------------------------------------------------

    def log(self, kind: str, **fields: Any) -> None:
        record = {
            "ts": self.deps.clock.now().isoformat(),
            "campaign_id": str(self.c.id),
            "kind": kind,
            **fields,
        }
        with contextlib.suppress(OSError):  # never let disk logging break a run
            self.deps.campaign_log.write(self.c.id, record)

    async def _event(self, type_: str, payload: dict[str, Any]) -> None:
        await _emit(
            self.deps, self.user, self.c.id, type_, {"campaign_id": str(self.c.id), **payload}
        )

    async def _state(self, **kwargs: Any) -> None:
        phase: CampaignPhase | None = kwargs.get("phase")
        async with self.deps.uow_factory(self.user) as uow:
            await uow.campaigns.update_state(
                self.user, self.c.id, counters=dict(self.counters), **kwargs
            )
            payload: dict[str, Any] = {
                "campaign_id": str(self.c.id),
                "counters": dict(self.counters),
            }
            if phase is not None:
                payload |= {
                    "phase": phase.value,
                    "status": (kwargs.get("status") or CampaignStatus.RUNNING).value,
                }
                if kwargs.get("error"):
                    payload["error"] = kwargs["error"]
            await uow.events.append(
                NewEvent(
                    user_id=self.user,
                    type="campaign.phase" if phase is not None else "campaign.counters",
                    payload=payload,
                    campaign_id=self.c.id,
                )
            )
            await uow.commit()
        if phase is not None:
            self.log(
                "phase", phase=phase.value, counters=dict(self.counters), error=kwargs.get("error")
            )

    async def _check_cancelled(self) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            status = await uow.campaigns.status_of(self.user, self.c.id)
        if status is CampaignStatus.CANCELLED:
            raise _CancelledError

    # -- playbooks -------------------------------------------------------------------------------

    async def _load_playbooks(self) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            self.playbooks = await uow.playbooks.all_for_user(self.user)
        self.log(
            "memory_loaded",
            domains=len(self.playbooks),
            dead_urls=sum(len(p.dead_urls) for p in self.playbooks.values()),
        )

    def _playbook(self, url: str) -> DomainPlaybook:
        domain = registrable_host(url) or "unknown"
        if domain not in self.playbooks:
            self.playbooks[domain] = DomainPlaybook.new(self.user, domain, self.deps.clock.now())
        return self.playbooks[domain]

    async def _save_playbooks(self) -> None:
        if not self.dirty:
            return
        async with self.deps.uow_factory(self.user) as uow:
            for domain in self.dirty:
                pb = self.playbooks[domain]
                pb.updated_at = self.deps.clock.now()
                await uow.playbooks.save(pb)
            await uow.commit()
        self.dirty.clear()

    def _touch(self, pb: DomainPlaybook) -> None:
        self.dirty.add(pb.domain)

    # -- run -------------------------------------------------------------------------------------

    async def execute(self) -> None:
        now = self.deps.clock.now()
        self.log(
            "start",
            name=self.c.name,
            keywords=self.c.keywords,
            scope=self.c.scope.value,
            sources=[{"type": s.source_type.value, "url": s.url} for s in self.c.sources],
            log_file=self.deps.campaign_log.path_for(self.c.id),
        )
        await self._state(
            status=CampaignStatus.RUNNING, phase=CampaignPhase.CRAWLING, started_at=now
        )
        try:
            await self._load_playbooks()
            llm = await self.deps.chat.execute(self.user)
            embedder = await self.deps.embeddings.execute(self.user)
            await self._crawl()
            await self._save_playbooks()
            await self._check_cancelled()
            await self._state(phase=CampaignPhase.EXTRACTING)
            found = await self._extract(llm, embedder)
            await self._save_playbooks()
            await self._check_cancelled()
            await self._state(phase=CampaignPhase.EVALUATING)
            eligible = await self._evaluate(llm, found)
            await self._check_cancelled()
            await self._state(phase=CampaignPhase.QUEUED)
            await self._queue(eligible)
            await self._state(
                status=CampaignStatus.COMPLETED,
                phase=CampaignPhase.COMPLETED,
                finished_at=self.deps.clock.now(),
            )
        except _CancelledError:
            self.log("cancelled")
            return
        except Exception as exc:  # noqa: BLE001 - any failure ends the campaign visibly
            message = (
                exc.message
                if isinstance(exc, DomainError)
                else f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            await self._save_playbooks()
            await self._state(
                status=CampaignStatus.FAILED,
                phase=CampaignPhase.FAILED,
                error=message[:500],
                finished_at=self.deps.clock.now(),
            )

    async def retry_page(self, page: CrawledPage) -> None:
        await self._load_playbooks()
        llm = await self.deps.chat.execute(self.user)
        embedder = await self.deps.embeddings.execute(self.user)
        pb = self._playbook(page.url)
        trace = Trace(page.trace)
        last = FetchStrategy.from_dict(page.strategy) if page.strategy else pb.fetch
        start = escalate_fetch(last, page.error_code or "network") or replace(
            last, level=last.level + 1
        )
        if page.error_stage == Stage.LLM_EXTRACTION.value:
            start = last  # fetching worked; only extraction needs a new approach
        trace.add(
            Stage.MEMORY,
            StepStatus.OK,
            action="manual_retry",
            previous_error=page.error_code,
            strategy=start.to_dict(),
        )
        self.log("retry", url=page.url, previous_error=page.error_code, strategy=start.to_dict())
        self.crawler = self.deps.crawler_factory.create()
        try:
            fetched = await self.crawler.fetch(page.url, start)
            fetched = await self._adaptive_fetch(fetched, pb, start)
        finally:
            await self.crawler.aclose()
        updated = self._page_from_fetch(
            fetched,
            page.source_id,
            page.discovery_source or "retry",
            page.discovery_query,
            Trace([*trace.steps, *fetched.trace]),
            page_id=page.id,
        )
        async with self.deps.uow_factory(self.user) as uow:
            await uow.pages.update_diagnostics(updated)
            await uow.commit()
        self.counters["pages_retried"] += 1
        await self._event(
            "page.retried",
            {"url": page.url, "status": updated.status.value, "error_code": updated.error_code},
        )
        found: list[Opportunity] = []
        if updated.status is CrawlStatus.DISTILLED:
            found = await self._extract_page(
                updated,
                llm,
                embedder,
                force_escalate=page.error_stage == Stage.LLM_EXTRACTION.value,
            )
        eligible = await self._evaluate(llm, found)
        await self._queue(eligible)
        await self._save_playbooks()
        async with self.deps.uow_factory(self.user) as uow:
            await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
            await uow.commit()

    # -- crawling --------------------------------------------------------------------------------

    async def _crawl(self) -> None:
        self.crawler = self.deps.crawler_factory.create()
        try:
            for source in self.c.sources:
                if not source.enabled:
                    continue
                if source.source_type is SourceType.WEBSITE and source.url:
                    pb = self._playbook(source.url)
                    site_strategy = pb.fetch if pb.fetch.level > 0 else None
                    if site_strategy is not None:
                        await self._adapted_notice(pb, source.url, site_strategy)
                    async for fetched in self.crawler.crawl(
                        source.url, self.c.keywords, self.c.settings, site_strategy
                    ):
                        disc: dict[str, Any] = next(
                            (
                                s["detail"]
                                for s in fetched.trace
                                if s["stage"] == Stage.DISCOVERY.value
                            ),
                            {},
                        )
                        await self._handle(
                            fetched, source.id, str(disc.get("source", "seed")), None
                        )
                        await self._check_cancelled()
                elif source.source_type is SourceType.WEB_SEARCH:
                    await self._search(source.id)
                elif source.source_type is SourceType.LINKEDIN_PUBLIC:
                    await self._linkedin(source.id)
        finally:
            await self.crawler.aclose()

    async def _adapted_notice(self, pb: DomainPlaybook, url: str, strategy: FetchStrategy) -> None:
        campaign = (
            f"#{pb.last_campaign_id.hex[-6:]}" if pb.last_campaign_id else "an earlier campaign"
        )
        message = (
            f"Adapted fetch strategy for {pb.domain} based on Campaign {campaign} failure memory"
        )
        self.counters["adapted"] += 1
        self.log("adapted", url=url, domain=pb.domain, strategy=strategy.to_dict(), message=message)
        await self._event(
            "page.adapted",
            {"url": url, "domain": pb.domain, "message": message, "strategy": strategy.to_dict()},
        )

    async def _search(self, source_id: uuid.UUID) -> None:
        assert self.crawler is not None
        queries = build_search_queries(self.c.keywords, self.c.scope, self.c.settings.home_country)
        seen: set[str] = set()
        for query in queries:
            hits = await self.deps.search.search(query, self.c.settings.search_results_per_query)
            self.log(
                "search", query=query, provider=self.deps.search.name, hits=[h.url for h in hits]
            )
            await self._event(
                "search.results",
                {"query": query, "hits": len(hits), "provider": self.deps.search.name},
            )
            for hit in hits:
                key = normalize_url(hit.url)
                if key in seen or len(seen) >= self.c.settings.max_search_pages:
                    continue
                seen.add(key)
                pb = self._playbook(hit.url)
                dead = pb.dead_reason(key)
                if dead is not None:
                    await self._skip_dead(hit.url, dead, source_id, query)
                    continue
                strategy = pb.fetch if pb.fetch.level > 0 else None
                if strategy is not None:
                    await self._adapted_notice(pb, hit.url, strategy)
                fetched = await self.crawler.fetch(hit.url, strategy)
                await self._handle(fetched, source_id, "web_search", query)
                await self._check_cancelled()

    async def _skip_dead(
        self, url: str, dead: dict[str, Any], source_id: uuid.UUID, query: str | None
    ) -> None:
        campaign = f"#{str(dead.get('campaign_id', ''))[-6:]}"
        message = f"Skipped based on Campaign {campaign} failure memory ({dead.get('code')})"
        trace = Trace()
        trace.add(Stage.DISCOVERY, StepStatus.OK, source="web_search", query=query)
        trace.add(
            Stage.MEMORY,
            StepStatus.SKIP,
            action="skipped_dead_url",
            reason=dead.get("code"),
            failed_in_campaign=dead.get("campaign_id"),
            message=message,
        )
        self.counters["pages_skipped"] += 1
        self.log("skipped", url=url, reason=dead, message=message)
        await self._save_page(
            CrawledPage(
                id=new_uuid7(),
                user_id=self.user,
                campaign_id=self.c.id,
                source_id=source_id,
                url=url[:2000],
                final_url=url[:2000],
                http_status=None,
                status=CrawlStatus.SKIPPED,
                title=None,
                content_sha256=None,
                markdown=None,
                injection_flags=[],
                error=message,
                fetched_at=self.deps.clock.now(),
                trace=trace.steps,
                error_stage=Stage.MEMORY.value,
                error_code=str(dead.get("code")),
                error_detail=message,
                discovery_source="web_search",
                discovery_query=query,
            ),
            event_extra={"message": message},
        )

    async def _linkedin(self, source_id: uuid.UUID) -> None:
        location = self.c.settings.home_country if self.c.scope is Scope.LOCAL else None
        for keyword in self.c.keywords:
            listings = await self.deps.listings.search(keyword, location, 10)
            self.log("linkedin", keyword=keyword, location=location, listings=len(listings))
            for listing in listings:
                markdown = "\n".join(
                    line
                    for line in (
                        f"# {listing.title}",
                        f"Organization: {listing.organization}" if listing.organization else "",
                        f"Location: {listing.location}" if listing.location else "",
                        f"Posted: {listing.posted}" if listing.posted else "",
                        f"Apply: {listing.url}",
                        f"Source: {listing.source}",
                    )
                    if line
                )
                trace = Trace()
                trace.add(
                    Stage.DISCOVERY,
                    StepStatus.OK,
                    source="linkedin",
                    query=keyword,
                    location=location,
                )
                trace.add(
                    Stage.DISTILL_AND_SANITIZE,
                    StepStatus.OK,
                    markdown_chars=len(markdown),
                    note="listing card",
                )
                await self._save_page(
                    CrawledPage(
                        id=new_uuid7(),
                        user_id=self.user,
                        campaign_id=self.c.id,
                        source_id=source_id,
                        url=listing.url,
                        final_url=listing.url,
                        http_status=200,
                        status=CrawlStatus.DISTILLED,
                        title=listing.title,
                        content_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
                        markdown=markdown,
                        injection_flags=[],
                        error=None,
                        fetched_at=self.deps.clock.now(),
                        trace=trace.steps,
                        discovery_source="linkedin",
                        discovery_query=keyword,
                    )
                )

    async def _adaptive_fetch(
        self, fetched: FetchedPage, pb: DomainPlaybook, strategy: FetchStrategy
    ) -> FetchedPage:
        """Retry transient fetch failures with escalated strategies; learn what worked."""
        assert self.crawler is not None
        steps = list(fetched.trace)
        code = fetch_error_code(fetched.status, fetched.error, len(fetched.html))
        retries = 0
        while code and not fetched.blocked and retries < 2:
            nxt = escalate_fetch(strategy, code)
            if nxt is None:
                break
            pb.record_failure(
                Stage.BROWSER_FETCH if strategy.mode == "browser" else Stage.HTTP_FETCH,
                code,
                self.c.id,
            )
            self._touch(pb)
            steps.append(
                {
                    "stage": Stage.MEMORY.value,
                    "status": StepStatus.OK.value,
                    "started_at": self.deps.clock.now().isoformat(),
                    "duration_ms": 0,
                    "detail": {
                        "action": "adaptive_retry",
                        "reason": code,
                        "next_strategy": nxt.to_dict(),
                    },
                }
            )
            self.counters["pages_retried"] += 1
            self.log("adaptive_retry", url=fetched.url, reason=code, strategy=nxt.to_dict())
            await self._event(
                "page.retrying", {"url": fetched.url, "reason": code, "strategy": nxt.to_dict()}
            )
            fetched = await self.crawler.fetch(fetched.url, nxt)
            steps.extend(fetched.trace)
            strategy = nxt
            code = fetch_error_code(fetched.status, fetched.error, len(fetched.html))
            retries += 1
        if code is None and strategy.level > 0:
            pb.fetch = strategy  # remember the strategy that worked for this domain
            self._touch(pb)
        return replace(fetched, trace=steps, strategy=strategy.to_dict())

    def _page_from_fetch(
        self,
        fetched: FetchedPage,
        source_id: uuid.UUID | None,
        discovery_source: str,
        discovery_query: str | None,
        trace: Trace,
        page_id: uuid.UUID | None = None,
    ) -> CrawledPage:
        pb = self._playbook(fetched.url)
        now = self.deps.clock.now()
        base = CrawledPage(
            id=page_id or new_uuid7(),
            user_id=self.user,
            campaign_id=self.c.id,
            source_id=source_id,
            url=fetched.url[:2000],
            final_url=(fetched.final_url or fetched.url)[:2000],
            http_status=fetched.status,
            status=CrawlStatus.PENDING,
            title=None,
            content_sha256=None,
            markdown=None,
            injection_flags=[],
            error=None,
            fetched_at=now,
            discovery_source=discovery_source,
            discovery_query=discovery_query,
            strategy=fetched.strategy,
        )
        code = fetch_error_code(fetched.status, fetched.error, len(fetched.html))
        if fetched.blocked:
            return replace(
                base,
                status=CrawlStatus.BLOCKED_SSRF,
                title=None,
                content_sha256=None,
                markdown=None,
                injection_flags=[],
                error=fetched.error or "blocked by SSRF guard",
                trace=trace.steps,
                error_stage=Stage.PREFLIGHT_AND_SSRF.value,
                error_code="ssrf_blocked",
                error_detail=fetched.error,
            )
        if code is not None and code != "empty_content":
            stage = (
                Stage.BROWSER_FETCH
                if fetched.strategy.get("mode", "browser") == "browser"
                else Stage.HTTP_FETCH
            )
            if code in PERMANENT_CODES:
                pb.mark_dead(normalize_url(fetched.url), code, self.c.id, now)
            else:
                pb.record_failure(stage, code, self.c.id)
            self._touch(pb)
            detail = fetched.error or f"HTTP {fetched.status}"
            return replace(
                base,
                status=CrawlStatus.FAILED,
                title=None,
                content_sha256=None,
                markdown=None,
                injection_flags=[],
                error=detail[:500],
                trace=trace.steps,
                error_stage=stage.value,
                error_code=code,
                error_detail=detail[:2000],
            )
        with trace.step(Stage.DISTILL_AND_SANITIZE, html_bytes=len(fetched.html.encode())) as step:
            processed = self.deps.processor.process(fetched.html, fetched.final_url)
            step.detail |= {
                "markdown_chars": len(processed.markdown),
                "title": processed.title,
                "injection_flags": len(processed.injection_flags),
                "flag_patterns": sorted({f["pattern"] for f in processed.injection_flags}),
                "sanitizer": "stripped scripts/styles/comments/hidden nodes/invisible chars; NFKC",
            }
            failure: str | None = None
            if len(processed.markdown) < MIN_MARKDOWN_CHARS:
                failure = "empty_content"
            elif len(processed.markdown) < 800 and _LOGIN_WALL.search(processed.markdown):
                failure = "login_wall"
            if failure:
                step.status = StepStatus.FAIL
                step.detail["error"] = failure
        if failure:
            if failure == "login_wall":
                pb.mark_dead(normalize_url(fetched.url), failure, self.c.id, now)
            else:
                pb.record_failure(Stage.DISTILL_AND_SANITIZE, failure, self.c.id)
            self._touch(pb)
            msg = (
                "page requires login"
                if failure == "login_wall"
                else f"only {len(processed.markdown)} readable chars (JS-rendered or blocked)"
            )
            return replace(
                base,
                status=CrawlStatus.FAILED,
                title=processed.title,
                content_sha256=processed.content_sha256,
                markdown=processed.markdown,
                injection_flags=processed.injection_flags,
                error=msg,
                trace=trace.steps,
                error_stage=Stage.DISTILL_AND_SANITIZE.value,
                error_code=failure,
                error_detail=msg,
            )
        pb.successes += 1
        self._touch(pb)
        return replace(
            base,
            status=CrawlStatus.DISTILLED,
            title=processed.title,
            content_sha256=processed.content_sha256,
            markdown=processed.markdown,
            injection_flags=processed.injection_flags,
            error=None,
            trace=trace.steps,
        )

    async def _handle(
        self, fetched: FetchedPage, source_id: uuid.UUID, discovery_source: str, query: str | None
    ) -> None:
        pb = self._playbook(fetched.url)
        strategy = (
            FetchStrategy.from_dict(fetched.strategy) if fetched.strategy else FetchStrategy()
        )
        steps = list(fetched.trace)
        if not any(s["stage"] == Stage.DISCOVERY.value for s in steps):
            steps.insert(
                0,
                {
                    "stage": Stage.DISCOVERY.value,
                    "status": StepStatus.OK.value,
                    "started_at": "",
                    "duration_ms": 0,
                    "detail": {"source": discovery_source, "query": query},
                },
            )
        fetched = await self._adaptive_fetch(replace(fetched, trace=steps), pb, strategy)
        page = self._page_from_fetch(
            fetched, source_id, discovery_source, query, Trace(fetched.trace)
        )
        await self._save_page(page)

    async def _save_page(
        self, page: CrawledPage, event_extra: dict[str, Any] | None = None
    ) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            inserted = await uow.pages.add(page)
            if inserted:
                if page.status is CrawlStatus.DISTILLED:
                    self.counters["websites_crawled"] += 1
                elif page.status is CrawlStatus.BLOCKED_SSRF:
                    self.counters["pages_blocked"] += 1
                elif page.status is CrawlStatus.FAILED:
                    self.counters["pages_failed"] += 1
                await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
                await uow.events.append(
                    NewEvent(
                        user_id=self.user,
                        type="page.skipped"
                        if page.status is CrawlStatus.SKIPPED
                        else "page.crawled",
                        campaign_id=self.c.id,
                        payload={
                            "campaign_id": str(self.c.id),
                            "page_id": str(page.id),
                            "url": page.final_url,
                            "status": page.status.value,
                            "injection_flags": len(page.injection_flags),
                            "error_stage": page.error_stage,
                            "error_code": page.error_code,
                            "counters": dict(self.counters),
                            **(event_extra or {}),
                        },
                    )
                )
            await uow.commit()
        self.log(
            "page",
            url=page.final_url,
            status=page.status.value,
            error_stage=page.error_stage,
            error_code=page.error_code,
            error=page.error,
            discovery_source=page.discovery_source,
            query=page.discovery_query,
            markdown_chars=len(page.markdown or ""),
            trace=page.trace,
            inserted=inserted,
        )

    # -- extraction ------------------------------------------------------------------------------

    async def _extract(self, llm: ILLMProvider, embedder: IEmbeddingProvider) -> list[Opportunity]:
        async with self.deps.uow_factory(self.user) as uow:
            pages = await uow.pages.for_campaign(self.user, self.c.id, [CrawlStatus.DISTILLED])
        found: list[Opportunity] = []
        for page in pages:
            await self._check_cancelled()
            found += await self._extract_page(page, llm, embedder)
        return found

    async def _extract_page(
        self,
        page: CrawledPage,
        llm: ILLMProvider,
        embedder: IEmbeddingProvider,
        *,
        force_escalate: bool = False,
    ) -> list[Opportunity]:
        pb = self._playbook(page.url)
        strategy = pb.extraction
        if force_escalate:
            strategy = escalate_extraction(strategy) or strategy
        if pb.extraction.level > 0:
            await self._adapted_notice(pb, page.url, FetchStrategy(level=0))
        trace = Trace(page.trace)
        markdown = page.markdown or ""
        result = None
        error: Exception | None = None
        for _ in range(3):
            with trace.step(
                Stage.LLM_EXTRACTION, strategy=strategy.to_dict(), input_chars=len(markdown)
            ) as step:
                try:
                    result = await self.deps.extractor.extract(
                        llm, markdown, page.final_url or page.url, strategy
                    )
                    step.detail |= result.diagnostics
                except DomainError as exc:
                    error = exc
                    step.status = StepStatus.FAIL
                    step.detail |= {"error": exc.message, **getattr(exc, "diagnostics", {})}
            if result is not None:
                break
            nxt = escalate_extraction(strategy)
            if nxt is None:
                break
            pb.record_failure(Stage.LLM_EXTRACTION, "invalid_json", self.c.id)
            self._touch(pb)
            trace.add(
                Stage.MEMORY,
                StepStatus.OK,
                action="adaptive_retry",
                reason="llm_extraction_failed",
                next_strategy=nxt.to_dict(),
            )
            self.log("extraction_retry", url=page.url, strategy=nxt.to_dict(), error=str(error))
            await self._event(
                "page.retrying",
                {"url": page.url, "reason": "llm_extraction", "strategy": nxt.to_dict()},
            )
            strategy = nxt
        page.trace = trace.steps
        if result is None:
            message = error.message if isinstance(error, DomainError) else "extraction failed"
            truncated = "length" in message
            page.status, page.error_stage = CrawlStatus.FAILED, Stage.LLM_EXTRACTION.value
            page.error_code = "llm_truncated" if truncated else "llm_invalid_json"
            page.error, page.error_detail = f"extraction: {message}"[:500], message[:2000]
            pb.record_failure(Stage.LLM_EXTRACTION, page.error_code, self.c.id)
            self._touch(pb)
            await self._persist_page(page)
            return []
        if strategy.level > pb.extraction.level:
            pb.extraction = strategy  # remember: this domain needs smaller windows / more tokens
            self._touch(pb)
        found: list[Opportunity] = []
        for extract in result.extracts:
            opp = await self._persist(extract, page, embedder)
            if opp is not None:
                found.append(opp)
        page.status, page.error, page.error_stage, page.error_code, page.error_detail = (
            CrawlStatus.EXTRACTED,
            None,
            None,
            None,
            None,
        )
        await self._persist_page(page)
        return found

    async def _persist_page(self, page: CrawledPage) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            await uow.pages.update_diagnostics(page)
            await uow.commit()
        self.log(
            "page_extraction",
            url=page.final_url,
            status=page.status.value,
            error_code=page.error_code,
            error=page.error,
            trace=[s for s in page.trace if s["stage"] == Stage.LLM_EXTRACTION.value],
        )

    async def _persist(
        self, ex: OpportunityExtract, page: CrawledPage, embedder: IEmbeddingProvider
    ) -> Opportunity | None:
        base = page.final_url or page.url
        portal = urljoin(base, ex.portal_url) if ex.portal_url else base
        if urlsplit(portal).scheme not in ("http", "https"):
            portal = base
        text = " | ".join(x for x in (ex.title, ex.organization or "", ex.summary[:500]) if x)
        vector = (await embedder.embed_documents([text]))[0]
        opp = Opportunity(
            id=new_uuid7(),
            user_id=self.user,
            campaign_id=self.c.id,
            page_id=page.id,
            type=ex.type,
            title=ex.title[:300],
            organization=(ex.organization or "")[:200] or None,
            location=(ex.location or "")[:200] or None,
            is_international=ex.is_international,
            deadline_at=deadline_to_datetime(ex.deadline),
            deadline_raw=(ex.deadline_raw or "")[:200] or None,
            portal_url=portal[:2000],
            source_url=base[:2000],
            summary=ex.summary[:4000],
            eligibility=ex.eligibility,
            ai_policy=ex.ai_policy,
            dedupe_key=dedupe_key(ex.title, ex.organization, ex.deadline),
            embedding_model=embedder.model_name,
            latest_outcome=None,
            created_at=self.deps.clock.now(),
        )
        async with self.deps.uow_factory(self.user) as uow:
            similar = await uow.opportunities.most_similar(self.user, vector, embedder.model_name)
            if similar is not None and similar[1] > DEDUPE_SIMILARITY:
                self.counters["duplicates"] += 1
                return None
            if not await uow.opportunities.add(opp, vector):
                self.counters["duplicates"] += 1
                return None
            self.counters["opportunities_found"] += 1
            await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
            await uow.events.append(
                NewEvent(
                    user_id=self.user,
                    type="opportunity.found",
                    campaign_id=self.c.id,
                    payload={
                        "campaign_id": str(self.c.id),
                        "opportunity_id": str(opp.id),
                        "title": opp.title,
                        "deadline": opp.deadline_at.date().isoformat() if opp.deadline_at else None,
                        "counters": dict(self.counters),
                    },
                )
            )
            await uow.commit()
        self.log("opportunity", title=opp.title, deadline=str(opp.deadline_at), url=base)
        return opp

    # -- eligibility -----------------------------------------------------------------------------

    async def _evaluate(
        self, llm: ILLMProvider, opportunities: list[Opportunity]
    ) -> list[Opportunity]:
        if not opportunities:
            return []
        async with self.deps.uow_factory(self.user) as uow:
            stored = await uow.profiles.get(self.user)
        profile = (
            stored.profile
            if stored
            else StudentProfile(
                id=uuid.UUID(int=0),
                user_id=self.user,
                full_name="",
                pii=StudentPII(),
                degree_level=None,
                major=None,
                institution=None,
                gpa=None,
                nationality=None,
                country_of_residence=None,
                graduation_year=None,
            )
        )
        snapshot = profile_snapshot(profile)
        snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        eligible: list[Opportunity] = []
        for opp in opportunities:
            await self._check_cancelled()
            now = self.deps.clock.now()
            results = evaluate_deterministic(opp.eligibility, profile, opp.deadline_at, now)
            outcome = decide(results)
            used_llm = False
            if outcome not in (EligibilityOutcome.EXPIRED, EligibilityOutcome.INELIGIBLE) and (
                opp.eligibility.other_criteria
            ):
                used_llm = True
                try:
                    results += await self.deps.fuzzy.evaluate(
                        llm, opp.eligibility.other_criteria, snapshot
                    )
                except DomainError as exc:
                    results += [
                        RuleResult(
                            c[:200],
                            RuleStatus.UNKNOWN,
                            f"LLM evaluation failed: {exc.message}",
                            RuleSource.LLM,
                        )
                        for c in opp.eligibility.other_criteria
                    ]
                outcome = decide(results)
            verdict = Verdict(
                id=new_uuid7(),
                user_id=self.user,
                opportunity_id=opp.id,
                outcome=outcome,
                reasons=results,
                profile_snapshot_hash=snapshot_hash,
                model=llm.model if used_llm else None,
                evaluated_at=now,
            )
            self.counters[outcome.value.lower()] += 1
            async with self.deps.uow_factory(self.user) as uow:
                await uow.verdicts.add(verdict)
                await uow.opportunities.set_outcome(self.user, opp.id, outcome)
                await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
                await uow.events.append(
                    NewEvent(
                        user_id=self.user,
                        type="eligibility.verdict",
                        campaign_id=self.c.id,
                        payload={
                            "campaign_id": str(self.c.id),
                            "opportunity_id": str(opp.id),
                            "title": opp.title,
                            "outcome": outcome.value,
                            "counters": dict(self.counters),
                        },
                    )
                )
                await uow.commit()
            self.log(
                "verdict",
                title=opp.title,
                outcome=outcome.value,
                reasons=[r.to_dict() for r in results],
            )
            if outcome is EligibilityOutcome.ELIGIBLE:
                eligible.append(opp)
        return eligible

    # -- queue -----------------------------------------------------------------------------------

    async def _queue(self, eligible: list[Opportunity]) -> None:
        for opp in sorted(eligible, key=_deadline_sort_key):
            now = self.deps.clock.now()
            app = Application(
                id=new_uuid7(),
                user_id=self.user,
                opportunity_id=opp.id,
                state=ApplicationState.ELIGIBLE,
                priority_rank=None,
                deadline_at=opp.deadline_at,
                created_at=now,
                updated_at=now,
            )
            transition = advance(
                app,
                ApplicationState.QUEUED,
                actor=Actor.SYSTEM,
                reason="eligible; queued by deadline",
                at=now,
            )
            async with self.deps.uow_factory(self.user) as uow:
                if not await uow.applications.add(app):
                    continue
                await uow.applications.add_transition(self.user, transition)
                await uow.applications.rerank_queue(self.user)
                self.counters["queued"] += 1
                await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
                await uow.events.append(
                    NewEvent(
                        user_id=self.user,
                        type="application.queued",
                        campaign_id=self.c.id,
                        application_id=app.id,
                        payload={
                            "campaign_id": str(self.c.id),
                            "opportunity_id": str(opp.id),
                            "title": opp.title,
                            "counters": dict(self.counters),
                            "deadline": opp.deadline_at.date().isoformat()
                            if opp.deadline_at
                            else None,
                        },
                    )
                )
                await uow.commit()
            self.log("queued", title=opp.title, deadline=str(opp.deadline_at))


def _deadline_sort_key(opp: Opportunity) -> tuple[int, datetime]:
    return (
        (0, opp.deadline_at)
        if opp.deadline_at
        else (1, datetime.max.replace(tzinfo=opp.created_at.tzinfo))
    )
