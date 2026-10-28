"""Discovery campaigns: create, run (Crawling -> Extracting -> Evaluating -> Queued), query."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
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
    is committed with a `pipeline_events` row (streamed over SSE)."""

    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            campaign = await uow.campaigns.get(user_id, campaign_id)
        if campaign is None or campaign.status is not CampaignStatus.DRAFT:
            return
        run = _Run(self.deps, campaign)
        await run.execute()


class _Run:
    def __init__(self, deps: DiscoveryDeps, campaign: Campaign) -> None:
        self.deps = deps
        self.c = campaign
        self.user = campaign.user_id
        self.counters = empty_counters()

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
            await uow.events.append(
                NewEvent(
                    user_id=self.user,
                    type="campaign.phase" if phase is not None else "campaign.counters",
                    payload=payload,
                    campaign_id=self.c.id,
                )
            )
            await uow.commit()

    async def _check_cancelled(self) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            status = await uow.campaigns.status_of(self.user, self.c.id)
        if status is CampaignStatus.CANCELLED:
            raise _CancelledError

    async def execute(self) -> None:
        now = self.deps.clock.now()
        await self._state(
            status=CampaignStatus.RUNNING, phase=CampaignPhase.CRAWLING, started_at=now
        )
        try:
            llm = await self.deps.chat.execute(self.user)
            embedder = await self.deps.embeddings.execute(self.user)
            await self._crawl()
            await self._check_cancelled()
            await self._state(phase=CampaignPhase.EXTRACTING)
            found = await self._extract(llm, embedder)
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
            return
        except Exception as exc:  # noqa: BLE001 - any failure ends the campaign visibly
            message = exc.message if isinstance(exc, DomainError) else f"{type(exc).__name__}"
            await self._state(
                status=CampaignStatus.FAILED,
                phase=CampaignPhase.FAILED,
                error=message[:500],
                finished_at=self.deps.clock.now(),
            )

    # -- crawling --------------------------------------------------------------------------------

    async def _crawl(self) -> None:
        crawler = self.deps.crawler_factory.create()
        try:
            for source in self.c.sources:
                if not source.enabled:
                    continue
                if source.source_type is SourceType.WEBSITE and source.url:
                    async for fetched in crawler.crawl(
                        source.url, self.c.keywords, self.c.settings
                    ):
                        await self._store(fetched, source.id)
                        await self._check_cancelled()
                elif source.source_type is SourceType.WEB_SEARCH:
                    await self._search(crawler, source.id)
                elif source.source_type is SourceType.LINKEDIN_PUBLIC:
                    await self._linkedin(source.id)
        finally:
            await crawler.aclose()

    async def _search(self, crawler: ISiteCrawler, source_id: uuid.UUID) -> None:
        queries = build_search_queries(self.c.keywords, self.c.scope, self.c.settings.home_country)
        seen: set[str] = set()
        for query in queries:
            hits = await self.deps.search.search(query, self.c.settings.search_results_per_query)
            await _emit(
                self.deps,
                self.user,
                self.c.id,
                "search.results",
                {"query": query, "hits": len(hits), "provider": self.deps.search.name},
            )
            for hit in hits:
                if hit.url in seen or len(seen) >= self.c.settings.max_search_pages:
                    continue
                seen.add(hit.url)
                await self._store(await crawler.fetch(hit.url), source_id)
                await self._check_cancelled()

    async def _linkedin(self, source_id: uuid.UUID) -> None:
        location = self.c.settings.home_country if self.c.scope is Scope.LOCAL else None
        for keyword in self.c.keywords:
            for listing in await self.deps.listings.search(keyword, location, 10):
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
                await self._save_page(
                    url=listing.url,
                    final_url=listing.url,
                    http_status=200,
                    status=CrawlStatus.DISTILLED,
                    title=listing.title,
                    markdown=markdown,
                    flags=[],
                    error=None,
                    source_id=source_id,
                )

    async def _store(self, fetched: FetchedPage, source_id: uuid.UUID) -> None:
        if fetched.blocked:
            await self._save_page(
                url=fetched.url,
                final_url=fetched.final_url,
                http_status=None,
                status=CrawlStatus.BLOCKED_SSRF,
                title=None,
                markdown=None,
                flags=[],
                error=fetched.error or "blocked by SSRF guard",
                source_id=source_id,
            )
            return
        if fetched.error or not fetched.html or (fetched.status or 0) >= 400:
            await self._save_page(
                url=fetched.url,
                final_url=fetched.final_url,
                http_status=fetched.status,
                status=CrawlStatus.FAILED,
                title=None,
                markdown=None,
                flags=[],
                error=fetched.error or f"HTTP {fetched.status}",
                source_id=source_id,
            )
            return
        processed = self.deps.processor.process(fetched.html, fetched.final_url)
        await self._save_page(
            url=fetched.url,
            final_url=fetched.final_url,
            http_status=fetched.status,
            status=CrawlStatus.DISTILLED,
            title=processed.title,
            markdown=processed.markdown,
            flags=processed.injection_flags,
            error=None,
            source_id=source_id,
            sha=processed.content_sha256,
        )

    async def _save_page(
        self,
        *,
        url: str,
        final_url: str | None,
        http_status: int | None,
        status: CrawlStatus,
        title: str | None,
        markdown: str | None,
        flags: list[dict[str, str]],
        error: str | None,
        source_id: uuid.UUID,
        sha: str | None = None,
    ) -> None:
        if sha is None and markdown:
            sha = hashlib.sha256(markdown.encode()).hexdigest()
        page = CrawledPage(
            id=new_uuid7(),
            user_id=self.user,
            campaign_id=self.c.id,
            source_id=source_id,
            url=url[:2000],
            final_url=(final_url or url)[:2000],
            http_status=http_status,
            status=status,
            title=(title or "")[:300] or None,
            content_sha256=sha,
            markdown=markdown,
            injection_flags=flags,
            error=(error or "")[:500] or None,
            fetched_at=self.deps.clock.now(),
        )
        async with self.deps.uow_factory(self.user) as uow:
            inserted = await uow.pages.add(page)
            if inserted:
                if status is CrawlStatus.DISTILLED:
                    self.counters["websites_crawled"] += 1
                elif status is CrawlStatus.BLOCKED_SSRF:
                    self.counters["pages_blocked"] += 1
                else:
                    self.counters["pages_failed"] += 1
                await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
                await uow.events.append(
                    NewEvent(
                        user_id=self.user,
                        type="page.crawled",
                        campaign_id=self.c.id,
                        payload={
                            "campaign_id": str(self.c.id),
                            "url": page.final_url,
                            "status": status.value,
                            "injection_flags": len(flags),
                            "counters": dict(self.counters),
                        },
                    )
                )
            await uow.commit()

    # -- extraction ------------------------------------------------------------------------------

    async def _extract(self, llm: ILLMProvider, embedder: IEmbeddingProvider) -> list[Opportunity]:
        async with self.deps.uow_factory(self.user) as uow:
            pages = await uow.pages.for_campaign(self.user, self.c.id, [CrawlStatus.DISTILLED])
        found: list[Opportunity] = []
        for page in pages:
            await self._check_cancelled()
            if not page.markdown or len(page.markdown) < MIN_MARKDOWN_CHARS:
                await self._page_status(page.id, CrawlStatus.EXTRACTED)
                continue
            try:
                extracts = await self.deps.extractor.extract(
                    llm, page.markdown, page.final_url or page.url
                )
            except DomainError as exc:
                await self._page_status(page.id, CrawlStatus.FAILED, f"extraction: {exc.message}")
                continue
            for extract in extracts:
                opp = await self._persist(extract, page, embedder)
                if opp is not None:
                    found.append(opp)
            await self._page_status(page.id, CrawlStatus.EXTRACTED)
        return found

    async def _page_status(
        self, page_id: uuid.UUID, status: CrawlStatus, error: str | None = None
    ) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            await uow.pages.set_status(self.user, page_id, status, error)
            await uow.commit()

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
        return opp

    # -- eligibility -----------------------------------------------------------------------------

    async def _evaluate(
        self, llm: ILLMProvider, opportunities: list[Opportunity]
    ) -> list[Opportunity]:
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
            # Stage 2 only for criteria Stage 1 cannot decide, and only if still viable.
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


def _deadline_sort_key(opp: Opportunity) -> tuple[int, datetime]:
    return (
        (0, opp.deadline_at)
        if opp.deadline_at
        else (1, datetime.max.replace(tzinfo=opp.created_at.tzinfo))
    )
