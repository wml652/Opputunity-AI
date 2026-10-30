"""The campaign run: a streaming crawl -> extract -> evaluate -> queue pipeline.

    producer (sources, hub detail links) --> asyncio.Queue --> N consumers
                                                               (extract, eligibility, queue)

* Pages stream into extraction as soon as they are distilled; verdicts and queued applications
  appear while crawling continues, so counters move continuously.
* Hub / listing pages are harvested deterministically: their best detail links (including
  off-site official programme pages behind a listicle) are fetched within a budget, and the LLM
  runs on the DETAIL pages. A hub is extracted directly only when its cards are self-contained.
* One failing page, window or source never aborts the campaign: errors are contained, recorded
  in the page trace and the flight recorder, and the run continues.
* Cancellation is honoured within about a second: a watcher polls the campaign status and
  cancels the run task; every fetch and every extraction window is also a checkpoint.
* Failure memory (domain playbooks) is saved immediately, domains in sorted order (concurrent
  runs can never deadlock on the upsert), including escalated strategies after a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from app.domain.applications.models import Application, advance
from app.domain.discovery.listing import (
    DedupeCandidate,
    HarvestedCard,
    PageKind,
    detail_link_score,
    has_distinct_identifiers,
    is_near_duplicate,
)
from app.domain.discovery.models import (
    DEDUPE_SIMILARITY,
    Campaign,
    CampaignPhase,
    CrawledPage,
    FetchedPage,
    Opportunity,
    OpportunityExtract,
    build_search_queries,
    deadline_to_datetime,
    dedupe_key,
    empty_counters,
    registrable_host,
)
from app.domain.discovery.playbook import (
    DOMAIN_BLOCKING_CODES,
    PERMANENT_CODES,
    DomainPlaybook,
    ExtractionStrategy,
    FetchStrategy,
    Stage,
    StepStatus,
    Trace,
    escalate_extraction,
    escalate_fetch,
    fetch_error_code,
    normalize_url,
)
from app.domain.discovery.progress import (
    EXTRACT_DONE,
    EXTRACT_QUEUED,
    PAGES_BUDGET,
    PAGES_FETCHED,
    STAGE_DONE,
    STAGE_READY,
    STAGE_TARGET,
    CampaignProgress,
    campaign_progress,
)
from app.domain.discovery.rag_filter import (
    INTENT_QUERY,
    MIN_FILTER_CHARS,
    SINGLE_WINDOW_CHARS,
    cosine,
    score_sections,
    select_sections,
    split_sections,
)
from app.domain.discovery.sessions import (
    BROWSER_SESSION_AAD,
    SEARCH_CREDENTIAL_AAD,
    SessionKind,
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
from app.domain.ports.crypto import aad
from app.domain.ports.discovery import (
    ISearchProvider,
    ISiteCrawler,
    Listing,
    ProcessedPage,
    Verdict,
)
from app.domain.ports.llm import (
    IEmbeddingProvider,
    ILLMProvider,
    IRerankProvider,
    IVisionProvider,
)
from app.domain.profile.models import StudentPII, StudentProfile
from app.domain.shared.enums import (
    Actor,
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
    Scope,
    SourceType,
)
from app.domain.shared.errors import DomainError
from app.domain.shared.ids import new_uuid7

if TYPE_CHECKING:
    from app.application.discovery.use_cases import DiscoveryDeps

MIN_MARKDOWN_CHARS = 120
VISION_MIN_CHARS = 1_500  # pages with less readable text are offered to a vision model
VISION_INSTRUCTION = (
    "Transcribe this web page screenshot into Markdown. Keep every opportunity's title, "
    "organization, deadline, eligibility and benefits; render tables as Markdown tables. "
    "Ignore navigation, ads and cookie banners. Output only the Markdown."
)
MIN_FOLLOWABLE_CARDS = 3  # a listing with fewer followable cards is extracted directly
KNOWN_TITLES_LIMIT = 2_000
CANCEL_POLL_S = 0.5
PROGRESS_INTERVAL_S = 1.5  # campaign.progress events at most this often (plus every phase change)
QUEUE_SIZE = 64

_LOGIN_WALL = re.compile(
    r"\b(sign in|log in|login|create an account)\b.{0,60}\b(continue|view|access|see)\b",
    re.I | re.S,
)


class CampaignCancelledError(Exception):
    """The user cancelled the campaign (raised at checkpoints)."""


@dataclass(slots=True)
class _Detail:
    url: str
    parent: str
    card: HarvestedCard
    score: int
    source_id: uuid.UUID | None


class CampaignRun:
    def __init__(self, deps: DiscoveryDeps, campaign: Campaign) -> None:
        self.deps = deps
        self.c = campaign
        self.user = campaign.user_id
        self.counters = {**empty_counters(), **campaign.counters}
        self.phase_value = campaign.phase.value
        self.progress_at = 0.0
        self.playbooks: dict[str, DomainPlaybook] = {}
        self.dirty: set[str] = set()
        self.pb_lock = asyncio.Lock()
        self.crawler: ISiteCrawler | None = None
        self.search: ISearchProvider = deps.search
        self.sessions: dict[str, str] = {}
        self.known: list[DedupeCandidate] = []
        self.seen_urls: set[str] = set()
        self.search_pages = 0
        self.details: deque[_Detail] = deque()
        self.detail_budget = campaign.settings.max_detail_pages
        self.queue: asyncio.Queue[CrawledPage | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.cancelled = False
        self._last_cancel_check = 0.0
        self.extract_llm: ILLMProvider | None = None
        self.primary_llm: ILLMProvider | None = None
        self.embedder: IEmbeddingProvider | None = None
        self.profile: StudentProfile | None = None
        self.snapshot: dict[str, Any] = {}
        self.snapshot_hash = ""
        self.home_country: str | None = campaign.settings.home_country
        self.reranker: IRerankProvider | None = None
        self.vision: IVisionProvider | None = None

    # -- flight recorder & events ----------------------------------------------------------------

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
        async with self.deps.uow_factory(self.user) as uow:
            await uow.events.append(
                NewEvent(
                    user_id=self.user,
                    type=type_,
                    campaign_id=self.c.id,
                    payload={"campaign_id": str(self.c.id), **payload},
                )
            )
            await uow.commit()

    async def _state(self, **kwargs: Any) -> None:
        phase: CampaignPhase | None = kwargs.get("phase")
        if phase is not None:
            self.phase_value = phase.value
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
                payload["progress"] = self._progress_view(None, phase.value).to_dict()
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

    # -- cancellation ----------------------------------------------------------------------------

    async def _is_cancelled(self) -> bool:
        async with self.deps.uow_factory(self.user) as uow:
            status = await uow.campaigns.status_of(self.user, self.c.id)
        return status is CampaignStatus.CANCELLED

    async def check_cancelled(self) -> None:
        """Checkpoint (before/after every fetch and every extraction window)."""
        if self.cancelled:
            raise CampaignCancelledError
        now = time.monotonic()
        if now - self._last_cancel_check < CANCEL_POLL_S:
            return
        self._last_cancel_check = now
        if await self._is_cancelled():
            self.cancelled = True
            raise CampaignCancelledError

    async def _watch_cancel(self, target: asyncio.Task[Any]) -> None:
        """Cancels the run task as soon as the campaign is cancelled, even mid-fetch or while
        waiting on a slow model."""
        while not target.done():
            await asyncio.sleep(CANCEL_POLL_S)
            try:
                if await self._is_cancelled():
                    self.cancelled = True
                    target.cancel()
                    return
            except Exception as exc:  # noqa: BLE001 - a DB hiccup must not kill the watcher
                self.log("cancel_watch_error", error=repr(exc)[:200])

    # -- playbooks -------------------------------------------------------------------------------

    def _playbook(self, url: str) -> DomainPlaybook:
        domain = registrable_host(url) or "unknown"
        if domain not in self.playbooks:
            self.playbooks[domain] = DomainPlaybook.new(self.user, domain, self.deps.clock.now())
        return self.playbooks[domain]

    def _touch(self, pb: DomainPlaybook) -> None:
        self.dirty.add(pb.domain)

    async def save_playbooks(self) -> None:
        """Upsert dirty playbooks in SORTED domain order (concurrent runs touching the same
        domains then lock rows in the same order and cannot deadlock)."""
        async with self.pb_lock:
            if not self.dirty:
                return
            domains, self.dirty = sorted(self.dirty), set()
            try:
                async with self.deps.uow_factory(self.user) as uow:
                    for domain in domains:
                        pb = self.playbooks[domain]
                        pb.updated_at = self.deps.clock.now()
                        await uow.playbooks.save(pb)
                    await uow.commit()
            except Exception:
                self.dirty.update(domains)  # retried at the next save
                raise

    # -- setup -----------------------------------------------------------------------------------

    async def prepare(self) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            self.playbooks = await uow.playbooks.all_for_user(self.user)
            stored_profile = await uow.profiles.get(self.user)
            sessions = await uow.browser_sessions.list(self.user)
            credentials = await uow.search_credentials.list(self.user)
            known = await uow.opportunities.recent_titles(self.user, KNOWN_TITLES_LIMIT)
        self.known = [DedupeCandidate(t, o, u) for _, t, o, u in known]
        self.profile = stored_profile.profile if stored_profile else _empty_profile(self.user)
        self.snapshot = profile_snapshot(self.profile)
        self.snapshot_hash = hashlib.sha256(
            json.dumps(self.snapshot, sort_keys=True).encode()
        ).hexdigest()
        if not self.home_country and self.profile.country_of_residence:
            self.home_country = self.profile.country_of_residence
        now = self.deps.clock.now()
        cipher = self.deps.cipher
        if cipher is not None:
            for s in sessions:
                if s.kind != SessionKind.CHALLENGE or s.is_expired(now):
                    continue
                with contextlib.suppress(Exception):
                    self.sessions[s.domain] = cipher.decrypt(
                        s.storage_state, aad(BROWSER_SESSION_AAD, "storage_state", s.user_id, s.id)
                    ).decode()
            if credentials and self.deps.search_factory is not None:
                keys: dict[str, str] = {}
                for cred in credentials:
                    with contextlib.suppress(Exception):
                        keys[cred.provider] = cipher.decrypt(
                            cred.key, aad(SEARCH_CREDENTIAL_AAD, "key", cred.user_id, cred.id)
                        ).decode()
                if keys:
                    self.search = self.deps.search_factory.create(
                        tavily_key=keys.get("tavily"), brave_key=keys.get("brave")
                    )
        self.primary_llm = await self.deps.chat.execute(self.user)
        self.extract_llm = await self.deps.chat.execute(self.user, purpose="extraction")
        self.embedder = await self.deps.embeddings.execute(self.user)
        self.log(
            "memory_loaded",
            domains=len(self.playbooks),
            dead_urls=sum(len(p.dead_urls) for p in self.playbooks.values()),
            challenged_domains=sorted(d for d, p in self.playbooks.items() if p.bot_challenge),
            solved_sessions=sorted(self.sessions),
            known_opportunities=len(self.known),
            home_country=self.home_country,
            extraction_model=self.extract_llm.model,
            primary_model=self.primary_llm.model,
            search=self.search.name,
        )
        if self.deps.rerank is not None:
            with contextlib.suppress(DomainError):
                self.reranker = await self.deps.rerank.execute(self.user)
        if self.deps.vision is not None:
            with contextlib.suppress(DomainError):
                self.vision = await self.deps.vision.execute(self.user)

    # -- run -------------------------------------------------------------------------------------

    async def execute(self) -> None:
        self.log(
            "start",
            name=self.c.name,
            keywords=self.c.keywords,
            scope=self.c.scope.value,
            sources=[{"type": s.source_type.value, "url": s.url} for s in self.c.sources],
            log_file=self.deps.campaign_log.path_for(self.c.id),
        )
        await self._state(
            status=CampaignStatus.RUNNING,
            phase=CampaignPhase.CRAWLING,
            started_at=self.deps.clock.now(),
        )
        run_task = asyncio.current_task()
        assert run_task is not None
        watcher = asyncio.create_task(self._watch_cancel(run_task))
        workers: list[asyncio.Task[None]] = []
        try:
            await self.prepare()
            self.counters[PAGES_BUDGET] = max(self.counters[PAGES_BUDGET], self._page_budget())
            n = max(1, min(4, self.c.settings.extraction_workers))
            workers = [asyncio.create_task(self._consume(i)) for i in range(n)]
            await self._produce()
            await self._state(phase=CampaignPhase.EXTRACTING)  # crawl done; draining the queue
            for _ in workers:
                await self.queue.put(None)
            await asyncio.gather(*workers)
            await self.save_playbooks()
            await self._auto_stage()
            await self._state(
                status=CampaignStatus.COMPLETED,
                phase=CampaignPhase.COMPLETED,
                finished_at=self.deps.clock.now(),
            )
        except (CampaignCancelledError, asyncio.CancelledError):
            await self._stop_workers(workers)
            with contextlib.suppress(Exception):
                await asyncio.shield(self.save_playbooks())
            with contextlib.suppress(Exception):
                await asyncio.shield(self._persist_counters())
            self.log("cancelled", counters=dict(self.counters))
            if not self.cancelled:
                raise  # app shutdown: the startup sweep marks the campaign interrupted
        except Exception as exc:  # noqa: BLE001 - any unexpected failure ends the run visibly
            await self._stop_workers(workers)
            message = (
                exc.message
                if isinstance(exc, DomainError)
                else f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            with contextlib.suppress(Exception):
                await self.save_playbooks()
            await self._state(
                status=CampaignStatus.FAILED,
                phase=CampaignPhase.FAILED,
                error=message[:500],
                finished_at=self.deps.clock.now(),
            )
        finally:
            watcher.cancel()
            with contextlib.suppress(BaseException):
                await watcher

    async def _auto_stage(self) -> None:
        """Fill this campaign's closest-deadline queued applications up to the final OK gate and
        wait for the filler (phase FILLING), so the campaign completes only after its staging
        pass. Never submits: each staged application still needs the human OK."""
        if not self.c.settings.auto_stage or self.deps.auto_stage is None:
            return
        await self._state(phase=CampaignPhase.FILLING)

        async def progress(done: int, total: int, current: str | None) -> None:
            self.counters[STAGE_TARGET], self.counters[STAGE_DONE] = total, done
            await self._progress(f"filling {current}" if current else None, force=True)
            await self.check_cancelled()

        try:
            states = await self.deps.auto_stage(
                self.user, self.c.id, self.c.settings.auto_stage_limit, progress
            )
        except DomainError as exc:
            self.log("auto_stage_failed", error=exc.message)
            return
        self.counters[STAGE_READY] = states.get("AWAITING_FINAL_VERIFICATION", 0)
        self.log("auto_stage", states=states)
        await self._event(
            "applications.auto_staged", {"count": sum(states.values()), "states": states}
        )
        if self.counters[STAGE_READY]:
            await self._state(phase=CampaignPhase.AWAITING_OK)

    def _page_budget(self) -> int:
        """Upper estimate of the pages this run may fetch (for the progress bar)."""
        st = self.c.settings
        budget = 0
        for source in self.c.sources:
            if not source.enabled:
                continue
            if source.source_type is SourceType.WEBSITE:
                budget += st.max_pages_per_site
            elif source.source_type is SourceType.WEB_SEARCH:
                budget += min(
                    st.max_search_queries * st.search_results_per_query, st.max_search_pages
                )
            else:
                budget += 10
        return budget + st.max_detail_pages

    def _progress_view(self, detail: str | None, phase: str | None = None) -> CampaignProgress:
        return campaign_progress(
            phase or self.phase_value,
            self.counters,
            auto_stage=self.c.settings.auto_stage and self.deps.auto_stage is not None,
            detail=detail,
        )

    async def _progress(self, detail: str | None, *, force: bool = False) -> None:
        """Throttled `campaign.progress` SSE event carrying both progress bars."""
        now = time.monotonic()
        if not force and now - self.progress_at < PROGRESS_INTERVAL_S:
            return
        self.progress_at = now
        await self._event("campaign.progress", self._progress_view(detail).to_dict())

    async def _enqueue(self, page: CrawledPage) -> None:
        self.counters[EXTRACT_QUEUED] += 1
        await self.queue.put(page)

    async def _stop_workers(self, workers: list[asyncio.Task[None]]) -> None:
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _persist_counters(self) -> None:
        async with self.deps.uow_factory(self.user) as uow:
            await uow.campaigns.update_state(self.user, self.c.id, counters=dict(self.counters))
            await uow.commit()

    # -- producer --------------------------------------------------------------------------------

    async def _produce(self) -> None:
        self.crawler = self.deps.crawler_factory.create(
            self.sessions, screenshots=self.vision is not None
        )
        try:
            for source in self.c.sources:
                if not source.enabled:
                    continue
                try:
                    if source.source_type is SourceType.WEBSITE and source.url:
                        await self._crawl_site(source.id, source.url)
                    elif source.source_type is SourceType.WEB_SEARCH:
                        await self._search(source.id)
                    elif source.source_type is SourceType.LINKEDIN_PUBLIC:
                        await self._linkedin(source.id)
                    elif source.source_type is SourceType.LINKEDIN_AUTH:
                        await self._linkedin_auth(source.id)
                    await self._drain_details()
                except (CampaignCancelledError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001 - one source never aborts the campaign
                    self.log(
                        "source_failed", source=source.source_type.value, error=repr(exc)[:300]
                    )
                    await self._event(
                        "source.failed",
                        {"source": source.source_type.value, "error": str(exc)[:200]},
                    )
        finally:
            crawler, self.crawler = self.crawler, None
            if crawler is not None:
                # EgressProxy.stop() is bounded (<= 2 s) - shutdown can no longer hang the run.
                await asyncio.shield(crawler.aclose())

    async def _crawl_site(self, source_id: uuid.UUID, url: str) -> None:
        assert self.crawler is not None
        if await self._skip_if_known_bad(url, source_id, "seed", None):
            return
        pb = self._playbook(url)
        site_strategy = pb.fetch if pb.fetch.level > 0 else None
        if site_strategy is not None:
            await self._adapted_notice(pb, url, site_strategy)
        async for fetched in self.crawler.crawl(
            url, self.c.keywords, self.c.settings, site_strategy
        ):
            await self.check_cancelled()
            disc: dict[str, Any] = next(
                (s["detail"] for s in fetched.trace if s["stage"] == Stage.DISCOVERY.value), {}
            )
            await self._handle(fetched, source_id, str(disc.get("source", "seed")), None)
            await self.check_cancelled()

    async def _search(self, source_id: uuid.UUID) -> None:
        assert self.crawler is not None
        queries = build_search_queries(
            self.c.keywords,
            self.c.scope,
            self.home_country,
            limit=self.c.settings.max_search_queries,
        )
        if self.c.scope in (Scope.LOCAL, Scope.BOTH) and not self.home_country:
            self.log("search_note", message="no home country known: local queries skipped")
        for query in queries:
            await self.check_cancelled()
            try:
                hits = await self.search.search(query, self.c.settings.search_results_per_query)
            except Exception as exc:  # noqa: BLE001 - a failing engine skips one query only
                self.log("search_failed", query=query, error=repr(exc)[:200])
                continue
            self.log("search", query=query, provider=self.search.name, hits=[h.url for h in hits])
            await self._event(
                "search.results", {"query": query, "hits": len(hits), "provider": self.search.name}
            )
            for hit in hits:
                key = normalize_url(hit.url)
                if key in self.seen_urls:
                    continue
                if self.search_pages >= self.c.settings.max_search_pages:
                    break
                self.search_pages += 1
                self.seen_urls.add(key)
                if await self._skip_if_known_bad(hit.url, source_id, "web_search", query):
                    continue
                await self._fetch_and_handle(hit.url, source_id, "web_search", query)
                await self._drain_details()

    async def _fetch_and_handle(
        self,
        url: str,
        source_id: uuid.UUID | None,
        discovery_source: str,
        query: str | None,
        discovery_detail: dict[str, Any] | None = None,
    ) -> None:
        assert self.crawler is not None
        await self.check_cancelled()
        pb = self._playbook(url)
        strategy = pb.fetch if pb.fetch.level > 0 else None
        if strategy is not None:
            await self._adapted_notice(pb, url, strategy)
        try:
            fetched = await self.crawler.fetch(url, strategy)
        except (CampaignCancelledError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - a crashing fetch becomes a failed page
            fetched = FetchedPage(url, url, None, "", error=f"network: {type(exc).__name__}")
        await self.check_cancelled()
        if discovery_detail:
            fetched = replace(
                fetched,
                trace=[
                    {
                        "stage": Stage.DISCOVERY.value,
                        "status": StepStatus.OK.value,
                        "started_at": "",
                        "duration_ms": 0,
                        "detail": {"source": discovery_source, "query": query, **discovery_detail},
                    },
                    *fetched.trace,
                ],
            )
        await self._handle(fetched, source_id, discovery_source, query)

    async def _drain_details(self) -> None:
        """Fetch the queued hub detail links (best first), within the campaign budget."""
        while self.details and self.detail_budget > 0:
            await self.check_cancelled()
            item = self.details.popleft()
            self.detail_budget -= 1
            if await self._skip_if_known_bad(item.url, item.source_id, "hub_detail", item.parent):
                continue
            await self._fetch_and_handle(
                item.url,
                item.source_id,
                "hub_detail",
                item.parent,
                {
                    "parent": item.parent,
                    "card_title": item.card.title,
                    "card_deadline": item.card.deadline_snippet,
                    "link_score": item.score,
                },
            )
        if self.details and self.detail_budget <= 0:
            self.log("detail_budget_exhausted", dropped=len(self.details))
            self.details.clear()

    # -- known-bad URLs (failure memory) ---------------------------------------------------------

    async def _skip_if_known_bad(
        self, url: str, source_id: uuid.UUID | None, discovery_source: str, query: str | None
    ) -> bool:
        pb = self._playbook(url)
        dead = pb.dead_reason(normalize_url(url))
        if dead is not None:
            campaign = f"#{str(dead.get('campaign_id', ''))[-6:]}"
            await self._skip(
                url,
                source_id,
                discovery_source,
                query,
                code=str(dead.get("code")),
                message=f"Skipped based on Campaign {campaign} failure memory ({dead.get('code')})",
                memory=dead,
            )
            return True
        if pb.bot_challenge and pb.domain not in self.sessions:
            campaign = f"#{str(pb.bot_challenge.get('campaign_id', ''))[-6:]}"
            await self._skip(
                url,
                source_id,
                discovery_source,
                query,
                code="bot_challenge",
                message=(
                    f"Skipped: {pb.domain} shows a {pb.bot_challenge.get('vendor', 'bot')} "
                    f"challenge (seen in Campaign {campaign}). Solve it once in Settings > "
                    "Browser Sessions to include this site."
                ),
                memory=pb.bot_challenge,
            )
            return True
        return False

    async def _skip(
        self,
        url: str,
        source_id: uuid.UUID | None,
        discovery_source: str,
        query: str | None,
        *,
        code: str,
        message: str,
        memory: dict[str, Any],
    ) -> None:
        trace = Trace()
        trace.add(Stage.DISCOVERY, StepStatus.OK, source=discovery_source, query=query)
        trace.add(
            Stage.MEMORY,
            StepStatus.SKIP,
            action="skipped_by_failure_memory",
            reason=code,
            failed_in_campaign=memory.get("campaign_id"),
            message=message,
        )
        self.counters["pages_skipped"] += 1
        self.log("skipped", url=url, reason=memory, message=message)
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
                error_code=code,
                error_detail=message,
                discovery_source=discovery_source,
                discovery_query=query,
            ),
            event_extra={"message": message},
        )

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

    async def _linkedin_auth(self, source_id: uuid.UUID) -> None:
        """Opt-in, signed-in LinkedIn search: one action per keyword page, never more than the
        user's daily cap; read-only (no posting, messaging or applying)."""
        adapter, cipher = self.deps.linkedin_auth, self.deps.cipher
        if adapter is None or cipher is None:
            self.log("linkedin_auth_skipped", reason="adapter not available")
            return
        async with self.deps.uow_factory(self.user) as uow:
            session = await uow.browser_sessions.find(
                self.user, "linkedin.com", SessionKind.LINKEDIN
            )
        now = self.deps.clock.now()
        if session is None or session.tos_acknowledged_at is None or session.is_expired(now):
            self.log("linkedin_auth_skipped", reason="no valid signed-in session")
            await self._event(
                "source.failed", {"source": "LINKEDIN_AUTH", "error": "sign in again"}
            )
            return
        state = cipher.decrypt(
            session.storage_state,
            aad(BROWSER_SESSION_AAD, "storage_state", self.user, session.id),
        ).decode()
        location = self.home_country if self.c.scope is Scope.LOCAL else None
        for keyword in self.c.keywords:
            await self.check_cancelled()
            today = self.deps.clock.now().date()
            if not session.can_act(today):
                self.log(
                    "linkedin_auth_cap",
                    cap=session.daily_action_cap,
                    used=session.actions_used(today),
                )
                break
            try:
                listings = await adapter.search(keyword, location, 25, state)
            finally:
                session.record_action(today, self.deps.clock.now())
                async with self.deps.uow_factory(self.user) as uow:
                    await uow.browser_sessions.update_usage(session)
                    await uow.commit()
            self.log("linkedin_auth", keyword=keyword, listings=len(listings))
            await self._save_listings(listings, source_id, "linkedin_auth", keyword, location)

    async def _linkedin(self, source_id: uuid.UUID) -> None:
        location = self.home_country if self.c.scope is Scope.LOCAL else None
        for keyword in self.c.keywords:
            await self.check_cancelled()
            listings = await self.deps.listings.search(keyword, location, 10)
            self.log("linkedin", keyword=keyword, location=location, listings=len(listings))
            await self._save_listings(listings, source_id, "linkedin", keyword, location)

    async def _save_listings(
        self,
        listings: list[Listing],
        source_id: uuid.UUID,
        source: str,
        keyword: str,
        location: str | None,
    ) -> None:
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
                Stage.DISCOVERY, StepStatus.OK, source=source, query=keyword, location=location
            )
            trace.add(
                Stage.DISTILL_AND_SANITIZE,
                StepStatus.OK,
                markdown_chars=len(markdown),
                note="listing card",
            )
            page = CrawledPage(
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
                discovery_source=source,
                discovery_query=keyword,
            )
            if await self._save_page(page):
                await self._enqueue(page)

    # -- fetch -> page ---------------------------------------------------------------------------

    async def adaptive_fetch(
        self, fetched: FetchedPage, pb: DomainPlaybook, strategy: FetchStrategy
    ) -> FetchedPage:
        """Retry transient failures with escalated strategies. The escalated strategy is
        remembered IMMEDIATELY (saved on failure too), so the next campaign starts there."""
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
            if nxt.level > pb.fetch.level:
                pb.fetch = nxt
            self._touch(pb)
            await self.save_playbooks()
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
            await self.check_cancelled()
            fetched = await self.crawler.fetch(fetched.url, nxt)
            steps.extend(fetched.trace)
            strategy = nxt
            code = fetch_error_code(fetched.status, fetched.error, len(fetched.html))
            retries += 1
        if code is None and strategy.level > 0:
            pb.fetch = strategy  # this domain needs the escalated strategy
            self._touch(pb)
        return replace(fetched, trace=steps, strategy=strategy.to_dict())

    def page_from_fetch(
        self,
        fetched: FetchedPage,
        source_id: uuid.UUID | None,
        discovery_source: str,
        discovery_query: str | None,
        trace: Trace,
        page_id: uuid.UUID | None = None,
    ) -> tuple[CrawledPage, ProcessedPage | None]:
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
        stage = (
            Stage.BROWSER_FETCH
            if fetched.strategy.get("mode", "browser") == "browser"
            else Stage.HTTP_FETCH
        )
        if fetched.blocked:
            return replace(
                base,
                status=CrawlStatus.BLOCKED_SSRF,
                error=fetched.error or "blocked by SSRF guard",
                trace=trace.steps,
                error_stage=Stage.PREFLIGHT_AND_SSRF.value,
                error_code="ssrf_blocked",
                error_detail=fetched.error,
            ), None
        if code in DOMAIN_BLOCKING_CODES:
            vendor = (fetched.error or "").split(":", 1)[-1].strip().split(" ", 1)[0] or "bot"
            pb.mark_challenged(vendor, fetched.url, self.c.id, now)
            pb.record_failure(stage, code, self.c.id)
            self._touch(pb)
            detail = fetched.error or "bot challenge"
            return replace(
                base,
                status=CrawlStatus.BLOCKED_BOT_CHALLENGE,
                error=detail[:500],
                trace=trace.steps,
                error_stage=stage.value,
                error_code=code,
                error_detail=(
                    f"{detail}. Automation never solves challenges: open Settings > Browser "
                    "Sessions and pass it once yourself to include this site."
                )[:2000],
            ), None
        if code is not None and code != "empty_content":
            if code in PERMANENT_CODES:
                pb.mark_dead(normalize_url(fetched.url), code, self.c.id, now)
            else:
                pb.record_failure(stage, code, self.c.id)
            self._touch(pb)
            detail = fetched.error or f"HTTP {fetched.status}"
            return replace(
                base,
                status=CrawlStatus.FAILED,
                error=detail[:500],
                trace=trace.steps,
                error_stage=Stage.PREFLIGHT_AND_SSRF.value if code == "dns_error" else stage.value,
                error_code=code,
                error_detail=detail[:2000],
            ), None
        with trace.step(Stage.DISTILL_AND_SANITIZE, html_bytes=len(fetched.html.encode())) as step:
            processed = self.deps.processor.process(fetched.html, fetched.final_url)
            step.detail |= {
                "markdown_chars": len(processed.markdown),
                "title": processed.title,
                "page_kind": processed.kind,
                "cards": len(processed.cards),
                "segmenter": processed.diagnostics,
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
            ), processed
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
        ), processed

    async def _handle(
        self,
        fetched: FetchedPage,
        source_id: uuid.UUID | None,
        discovery_source: str,
        query: str | None,
    ) -> None:
        """fetch result -> adaptive retries -> distill -> persist -> hub harvest / extraction
        queue. Errors here fail THIS page only."""
        try:
            await self._handle_page(fetched, source_id, discovery_source, query)
        except (CampaignCancelledError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - contain it: record and continue
            self.counters["pages_failed"] += 1
            self.log("page_error", url=fetched.url, error=repr(exc)[:300])
            await self._event(
                "page.crawled",
                {"url": fetched.url, "status": "FAILED", "error_code": "internal_error"},
            )
        finally:
            self.seen_urls.add(normalize_url(fetched.final_url or fetched.url))
            if self.dirty:
                with contextlib.suppress(Exception):
                    await self.save_playbooks()

    async def _handle_page(
        self,
        fetched: FetchedPage,
        source_id: uuid.UUID | None,
        discovery_source: str,
        query: str | None,
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
        if _looks_like_pdf(fetched):
            await self._handle_pdf(fetched, source_id, discovery_source, query)
            return
        fetched = await self.adaptive_fetch(replace(fetched, trace=steps), pb, strategy)
        page, processed = self.page_from_fetch(
            fetched, source_id, discovery_source, query, Trace(fetched.trace)
        )
        if fetched.screenshot is not None and self.vision is not None:
            enriched = await self._vision_parse(page, fetched.screenshot)
            if enriched is not None:
                if await self._save_page(enriched):
                    await self._enqueue(enriched)
                return
        if page.status is not CrawlStatus.DISTILLED or processed is None:
            await self._save_page(page)
            return
        hub_cards = self._followable_cards(processed, page, discovery_source)
        is_hub = processed.kind == PageKind.LISTING and len(hub_cards) >= MIN_FOLLOWABLE_CARDS
        if is_hub:
            page = self._mark_harvested(page, processed, hub_cards)
            if await self._save_page(page):
                self._queue_details(page, hub_cards, source_id)
            return
        if not await self._save_page(page):
            return  # identical content already stored in this campaign
        if processed.kind == PageKind.MIXED and hub_cards:
            self._queue_details(page, hub_cards, source_id)
        await self._enqueue(page)

    # -- hub harvesting --------------------------------------------------------------------------

    def _followable_cards(
        self, processed: ProcessedPage, page: CrawledPage, discovery_source: str
    ) -> list[tuple[HarvestedCard, int]]:
        hub_url = page.final_url or page.url
        hub_site = registrable_host(hub_url)
        from_site_crawl = discovery_source in ("seed", "followed_link")
        out: list[tuple[HarvestedCard, int]] = []
        for card in processed.cards:
            if not card.url:
                continue
            if from_site_crawl and registrable_host(card.url) == hub_site:
                continue  # the site crawler follows same-site links itself
            key = normalize_url(card.url)
            if key in self.seen_urls or key == normalize_url(hub_url):
                continue
            score = detail_link_score(card.url, card.title, self.c.keywords, hub_url)
            if score > 0:
                out.append((card, score))
        out.sort(key=lambda cs: -cs[1])
        return out

    def _mark_harvested(
        self,
        page: CrawledPage,
        processed: ProcessedPage,
        cards: list[tuple[HarvestedCard, int]],
    ) -> CrawledPage:
        per_hub = self.c.settings.max_details_per_hub
        queued = min(len(cards), per_hub, max(0, self.detail_budget - len(self.details)))
        trace = Trace(page.trace)
        trace.add(
            Stage.HUB_HARVEST,
            StepStatus.OK,
            page_kind=processed.kind,
            cards=len(processed.cards),
            followable=len(cards),
            queued=queued,
            not_followed=len(cards) - queued,
            top=[{"title": c.title, "url": c.url, "score": s} for c, s in cards[:queued]],
            note="hub/listing page: the LLM runs on the detail pages, not on the hub",
        )
        self.counters["hubs_harvested"] += 1
        self.counters["cards_harvested"] += len(processed.cards)
        return replace(page, status=CrawlStatus.EXTRACTED, trace=trace.steps)

    def _queue_details(
        self, page: CrawledPage, cards: list[tuple[HarvestedCard, int]], source_id: uuid.UUID | None
    ) -> None:
        room = max(0, self.detail_budget - len(self.details))
        take = cards[: min(self.c.settings.max_details_per_hub, room)]
        for card, score in take:
            assert card.url is not None
            key = normalize_url(card.url)
            if key in self.seen_urls:
                continue
            self.seen_urls.add(key)
            self.details.append(
                _Detail(card.url, page.final_url or page.url, card, score, source_id)
            )
        self.counters["details_queued"] += len(take)
        self.log(
            "hub_harvest",
            url=page.final_url,
            cards=len(cards),
            queued=[c.url for c, _ in take],
        )

    # -- persistence -----------------------------------------------------------------------------

    async def _save_page(
        self, page: CrawledPage, event_extra: dict[str, Any] | None = None
    ) -> bool:
        async with self.deps.uow_factory(self.user) as uow:
            inserted = await uow.pages.add(page)
            if inserted:
                self.counters[PAGES_FETCHED] += 1
                if page.status in (CrawlStatus.DISTILLED, CrawlStatus.EXTRACTED):
                    self.counters["websites_crawled"] += 1
                elif page.status is CrawlStatus.BLOCKED_SSRF:
                    self.counters["pages_blocked"] += 1
                elif page.status is CrawlStatus.BLOCKED_BOT_CHALLENGE:
                    self.counters["bot_challenges"] += 1
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
        if inserted:
            await self._progress(f"crawled {page.final_url}")
        return inserted

    async def persist_page(self, page: CrawledPage) -> None:
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

    # -- consumers -------------------------------------------------------------------------------

    async def _consume(self, worker: int) -> None:
        while True:
            page = await self.queue.get()
            if page is None:
                return
            try:
                await self.check_cancelled()
                found = await self.extract_page(page)
                eligible = await self.evaluate(found)
                await self.enqueue_applications(eligible)
                self.counters[EXTRACT_DONE] += 1
                await self._progress(f"extracted {page.final_url or page.url}")
            except (CampaignCancelledError, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - one page never stops a worker
                self.log("extraction_error", worker=worker, url=page.url, error=repr(exc)[:300])
                self.counters[EXTRACT_DONE] += 1
                page.status = CrawlStatus.FAILED
                page.error_stage, page.error_code = Stage.LLM_EXTRACTION.value, "internal_error"
                page.error = page.error_detail = f"extraction crashed: {type(exc).__name__}"
                with contextlib.suppress(Exception):
                    await self.persist_page(page)

    async def extract_page(
        self, page: CrawledPage, *, force_escalate: bool = False
    ) -> list[Opportunity]:
        assert self.extract_llm is not None
        pb = self._playbook(page.url)
        strategy: ExtractionStrategy = pb.extraction
        if force_escalate:
            strategy = escalate_extraction(strategy) or strategy
        if pb.extraction.level > 0:
            await self._adapted_notice(pb, page.url, FetchStrategy(level=0))
        trace = Trace(page.trace)
        markdown = await self._rag_filter(page.markdown or "", trace)
        if len(markdown) <= SINGLE_WINDOW_CHARS < strategy.chunk_chars * 2:
            # Everything relevant fits one window: one LLM call instead of several.
            strategy = replace(strategy, chunk_chars=max(strategy.chunk_chars, len(markdown) + 1))
        result = None
        error: Exception | None = None
        for _ in range(3):
            await self.check_cancelled()
            with trace.step(
                Stage.LLM_EXTRACTION, strategy=strategy.to_dict(), input_chars=len(markdown)
            ) as step:
                try:
                    result = await self.deps.extractor.extract(
                        self.extract_llm,
                        markdown,
                        page.final_url or page.url,
                        strategy,
                        checkpoint=self.check_cancelled,
                    )
                    step.detail |= result.diagnostics
                    if result.diagnostics.get("truncated_windows"):
                        step.detail["note"] = result.diagnostics.get("warning")
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
            if nxt.level > pb.extraction.level:
                pb.extraction = nxt  # remembered at once, also after a failure
            self._touch(pb)
            await self.save_playbooks()
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
            page.status, page.error_stage = CrawlStatus.FAILED, Stage.LLM_EXTRACTION.value
            page.error_code = (
                "llm_truncated"
                if "length" in message
                else "llm_timeout"
                if "timed out" in message
                else "llm_invalid_json"
            )
            page.error, page.error_detail = f"extraction: {message}"[:500], message[:2000]
            pb.record_failure(Stage.LLM_EXTRACTION, page.error_code, self.c.id)
            self._touch(pb)
            await self.persist_page(page)
            return []
        if strategy.level > pb.extraction.level:
            pb.extraction = strategy
            self._touch(pb)
        found: list[Opportunity] = []
        for extract in result.extracts:
            opp = await self._persist_opportunity(extract, page)
            if opp is not None:
                found.append(opp)
        page.status, page.error, page.error_stage, page.error_code, page.error_detail = (
            CrawlStatus.EXTRACTED,
            None,
            None,
            None,
            None,
        )
        await self.persist_page(page)
        return found

    async def _persist_opportunity(
        self, ex: OpportunityExtract, page: CrawledPage
    ) -> Opportunity | None:
        assert self.embedder is not None
        base = page.final_url or page.url
        portal = urljoin(base, ex.portal_url) if ex.portal_url else base
        if urlsplit(portal).scheme not in ("http", "https"):
            portal = base
        # The page URL is only a fallback portal (many items share it), never evidence that two
        # items are the same opportunity.
        candidate = DedupeCandidate(ex.title, ex.organization, portal if portal != base else None)
        if any(is_near_duplicate(candidate, k) for k in self.known):
            self.counters["duplicates"] += 1
            return None
        text = " | ".join(x for x in (ex.title, ex.organization or "", ex.summary[:500]) if x)
        vector = (await self.embedder.embed_documents([text]))[0]
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
            embedding_model=self.embedder.model_name,
            latest_outcome=None,
            created_at=self.deps.clock.now(),
        )
        async with self.deps.uow_factory(self.user) as uow:
            similar = await uow.opportunities.most_similar(
                self.user, vector, self.embedder.model_name
            )
            if similar is not None and similar[1] > DEDUPE_SIMILARITY:
                other = await uow.opportunities.get(self.user, similar[0])
                if other is None or not has_distinct_identifiers(opp.title, other.title):
                    self.counters["duplicates"] += 1
                    return None
            if not await uow.opportunities.add(opp, vector):
                self.counters["duplicates"] += 1
                return None
            self.known.append(candidate)
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

    async def evaluate(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        assert self.primary_llm is not None
        assert self.profile is not None
        eligible: list[Opportunity] = []
        for opp in opportunities:
            await self.check_cancelled()
            now = self.deps.clock.now()
            results = evaluate_deterministic(opp.eligibility, self.profile, opp.deadline_at, now)
            outcome = decide(results)
            used_llm = False
            if outcome not in (EligibilityOutcome.EXPIRED, EligibilityOutcome.INELIGIBLE) and (
                opp.eligibility.other_criteria
            ):
                used_llm = True
                try:
                    results += await self.deps.fuzzy.evaluate(
                        self.primary_llm, opp.eligibility.other_criteria, self.snapshot
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
                profile_snapshot_hash=self.snapshot_hash,
                model=self.primary_llm.model if used_llm else None,
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

    async def enqueue_applications(self, eligible: list[Opportunity]) -> None:
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

    # -- M6: in-page RAG, PDF documents, vision parsing -----------------------------------------

    async def _rag_filter(self, markdown: str, trace: Trace) -> str:
        """Send only the high-signal sections of a long page to the extraction model."""
        if len(markdown) <= MIN_FILTER_CHARS:
            return markdown
        started = time.perf_counter()
        sections = split_sections(markdown)
        cosines: list[float] | None = None
        rerank_scores: list[float] | None = None
        notes: list[str] = []
        query = f"{INTENT_QUERY}. Keywords: {', '.join(self.c.keywords)}"
        passages = [f"{s.heading}\n{s.text}"[:1_500] for s in sections]
        if self.embedder is not None and sections:
            try:
                qv = await self.embedder.embed_query(query)
                vectors = await self.embedder.embed_documents(passages)
                cosines = [cosine(qv, v) for v in vectors]
            except DomainError as exc:
                notes.append(f"embedding scores skipped: {exc.message}")
        reranker = self.reranker
        if reranker is not None and sections:
            try:
                rerank_scores = await reranker.rerank(query, passages)
            except DomainError as exc:
                notes.append(f"reranker failed, local scoring used: {exc.message}")
        selection = select_sections(
            score_sections(sections, self.c.keywords, cosines=cosines, rerank=rerank_scores)
        )
        trace.add(
            Stage.RAG_FILTER,
            StepStatus.OK,
            rag_sections_total=selection.sections_total,
            rag_sections_kept=selection.sections_kept,
            chars_before=selection.chars_before,
            chars_after=selection.chars_after,
            chars_saved_pct=selection.chars_saved_pct,
            single_window=selection.single_window,
            scorer=(
                f"rerank:{reranker.model} + local hybrid"
                if rerank_scores is not None and reranker is not None
                else "local hybrid (bm25 + signals" + (" + cosine)" if cosines else ")")
            ),
            kept=[k.section.heading or k.section.text[:60] for k in selection.kept[:12]],
            dropped=[d.section.heading or d.section.text[:60] for d in selection.dropped[:12]],
            duration_ms=int((time.perf_counter() - started) * 1000),
            notes=notes,
        )
        return selection.text

    async def _handle_pdf(
        self,
        fetched: FetchedPage,
        source_id: uuid.UUID | None,
        discovery_source: str,
        query: str | None,
    ) -> None:
        """An opportunity published as a PDF (call / brochure): download, parse, extract."""
        assert self.crawler is not None
        trace = Trace(list(fetched.trace))
        document = await self.crawler.fetch_document(fetched.url)
        trace.extend(document.trace)
        page = CrawledPage(
            id=new_uuid7(),
            user_id=self.user,
            campaign_id=self.c.id,
            source_id=source_id,
            url=fetched.url[:2000],
            final_url=document.final_url[:2000],
            http_status=document.status,
            status=CrawlStatus.FAILED,
            title=None,
            content_sha256=None,
            markdown=None,
            injection_flags=[],
            error=None,
            fetched_at=self.deps.clock.now(),
            discovery_source=discovery_source,
            discovery_query=query,
        )
        parser = self.deps.layout
        if document.error or not document.data or parser is None:
            reason = document.error or ("no PDF parser" if parser is None else "empty download")
            page = replace(
                page,
                error=reason,
                error_stage=Stage.PDF_PARSE.value,
                error_code="pdf_unavailable",
                error_detail=reason,
                trace=trace.steps,
            )
            await self._save_page(page)
            return
        with trace.step(Stage.PDF_PARSE, bytes=len(document.data)) as step:
            try:
                parsed = await parser.parse_pdf(document.data)
                step.detail |= {
                    "pages": parsed.pages,
                    "markdown_chars": len(parsed.markdown),
                    "method": parsed.method,
                }
            except DomainError as exc:
                step.status = StepStatus.FAIL
                step.detail["error"] = exc.message
                parsed = None
        if parsed is None or len(parsed.markdown) < MIN_MARKDOWN_CHARS:
            msg = "the PDF has no extractable text (scanned images?)"
            await self._save_page(
                replace(
                    page,
                    error=msg,
                    error_stage=Stage.PDF_PARSE.value,
                    error_code="empty_content",
                    error_detail=msg,
                    trace=trace.steps,
                )
            )
            return
        markdown = parsed.markdown[:80_000]
        page = replace(
            page,
            status=CrawlStatus.DISTILLED,
            title=fetched.url.rsplit("/", 1)[-1][:300],
            content_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            markdown=markdown,
            trace=trace.steps,
        )
        if await self._save_page(page):
            await self._enqueue(page)

    async def _vision_parse(self, page: CrawledPage, screenshot: bytes) -> CrawledPage | None:
        """Text-sparse page (posters, image tables): let the USER's vision model read it."""
        if page.status not in (CrawlStatus.DISTILLED, CrawlStatus.FAILED):
            return None
        if page.status is CrawlStatus.FAILED and page.error_code != "empty_content":
            return None
        if page.status is CrawlStatus.DISTILLED and len(page.markdown or "") >= VISION_MIN_CHARS:
            return None
        assert self.vision is not None
        trace = Trace(page.trace)
        with trace.step(Stage.VISION_PARSE, model=self.vision.model, bytes=len(screenshot)) as step:
            try:
                text = await self.vision.read_image(screenshot, "image/jpeg", VISION_INSTRUCTION)
            except DomainError as exc:
                step.status = StepStatus.FAIL
                step.detail["error"] = exc.message
                return None
            step.detail["markdown_chars"] = len(text)
        if len(text.strip()) < MIN_MARKDOWN_CHARS:
            return None
        markdown = ((page.markdown or "") + "\n\n" + text.strip()).strip()[:80_000]
        return replace(
            page,
            status=CrawlStatus.DISTILLED,
            markdown=markdown,
            content_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            error=None,
            error_stage=None,
            error_code=None,
            error_detail=None,
            trace=trace.steps,
        )

    # -- manual retry (Crawl Inspector) ----------------------------------------------------------

    async def retry_page(self, page: CrawledPage) -> None:
        await self.prepare()
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
        self.crawler = self.deps.crawler_factory.create(self.sessions)
        try:
            fetched = await self.crawler.fetch(page.url, start)
            fetched = await self.adaptive_fetch(fetched, pb, start)
        finally:
            await self.crawler.aclose()
            self.crawler = None
        updated, _ = self.page_from_fetch(
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
            found = await self.extract_page(
                updated, force_escalate=page.error_stage == Stage.LLM_EXTRACTION.value
            )
        eligible = await self.evaluate(found)
        await self.enqueue_applications(eligible)
        await self.save_playbooks()
        await self._persist_counters()


def _looks_like_pdf(fetched: FetchedPage) -> bool:
    path = fetched.url.split("?", 1)[0].lower()
    error = (fetched.error or "").lower()
    return (
        path.endswith(".pdf")
        or "application/pdf" in error
        or ("download is starting" in error and path.endswith((".pdf", "/download")))
    )


def _empty_profile(user_id: uuid.UUID) -> StudentProfile:
    return StudentProfile(
        id=uuid.UUID(int=0),
        user_id=user_id,
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


def _deadline_sort_key(opp: Opportunity) -> tuple[int, datetime]:
    return (
        (0, opp.deadline_at)
        if opp.deadline_at
        else (1, datetime.max.replace(tzinfo=opp.created_at.tzinfo))
    )
