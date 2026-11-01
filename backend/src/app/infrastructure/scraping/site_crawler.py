"""Polite, bounded, best-first site crawler.

* Stays on the seed's registrable domain (and its subdomains).
* Follows only links that look relevant (`detail_link_score`): opportunity detail slugs and the
  user's keywords first; the first index pages only (never `/page/38/`); no taxonomy/social
  links. Highest-scoring links are fetched first, up to `max_depth` and `max_pages_per_site`.
* Honors robots.txt (fetched through the SSRF-safe client) and waits `politeness_seconds`
  between requests to the same host.
"""

from __future__ import annotations

import asyncio
import heapq
import re
import time
import urllib.robotparser
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from app.domain.discovery.listing import detail_link_score
from app.domain.discovery.models import CampaignSettings, FetchedPage, registrable_host
from app.domain.discovery.playbook import FetchStrategy, Stage, StepStatus
from app.domain.ports.discovery import FetchedDocument
from app.domain.ports.system import IBrowserSlots
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.distiller import extract_links
from app.infrastructure.scraping.fetchers import PageFetcher, PlaywrightFetcher, SafeHttpFetcher
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

_SKIP_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|zip|rar|7z|gz|mp4|mp3|avi|mov|woff2?|ttf|xml|json|"
    r"docx?|xlsx?|pptx?|pdf)$",
    re.I,
)


def _normalize(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def link_score(url: str, anchor: str, keywords: Sequence[str], seed: str = "") -> int:
    """Best-first priority of a link (<= 0: not followed). Delegates to the domain scorer, which
    boosts opportunity detail slugs and keyword matches and penalizes pagination beyond page 2
    (`/page/38/`), taxonomy pages (`/tag/`, `/author/`, bare categories) and social links."""
    return detail_link_score(url, anchor, list(keywords), seed or url)


def same_site(url: str, seed: str) -> bool:
    host, root = registrable_host(url), registrable_host(seed)
    return bool(host) and (host == root or host.endswith("." + root))


class SiteCrawler:
    def __init__(
        self,
        *,
        page_fetcher: PageFetcher,
        http: SafeHttpFetcher,
        guard: SsrfGuard,
        politeness_seconds: float = 1.0,
        respect_robots: bool = True,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._http = http
        self._guard = guard
        self._delay = politeness_seconds
        self._respect_robots = respect_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def _polite(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_hit.get(host)
        if last is not None:
            wait = self._delay - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_hit[host] = time.monotonic()

    async def _allowed(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: urllib.robotparser.RobotFileParser | None = None
            status, _, text = await self._http.get_text(origin + "/robots.txt")
            if status == 200 and text:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch("*", url)

    async def fetch(self, url: str, strategy: FetchStrategy | None = None) -> FetchedPage:
        await self._polite(url)
        return await self._page_fetcher.fetch(url, strategy)

    async def crawl(
        self,
        seed_url: str,
        keywords: Sequence[str],
        settings: CampaignSettings,
        strategy: FetchStrategy | None = None,
    ) -> AsyncIterator[FetchedPage]:
        seed = _normalize(seed_url)
        frontier: list[tuple[int, int, int, str]] = [(0, 0, 0, seed)]  # (-score, depth, order, url)
        origin: dict[str, tuple[str, str]] = {}  # url -> (parent url, anchor text)
        seen = {seed}
        order = 0
        fetched = 0
        while frontier and fetched < settings.max_pages_per_site:
            neg_score, depth, _, url = heapq.heappop(frontier)
            parent, anchor = origin.get(url, ("", ""))
            # robots.txt governs links we FOLLOW; the seed is the user's explicit choice.
            if depth > 0 and not await self._allowed(url):
                log.info("crawler.robots_disallowed", url=url)
                continue
            page = await self.fetch(url, strategy)
            discovery = {
                "stage": Stage.DISCOVERY.value,
                "status": StepStatus.OK.value,
                "started_at": "",
                "duration_ms": 0,
                "detail": {
                    "source": "seed" if depth == 0 else "followed_link",
                    "seed": seed,
                    "depth": depth,
                    "parent": parent or None,
                    "anchor": anchor or None,
                    "link_score": -neg_score,
                },
            }
            page = replace(page, trace=[discovery, *page.trace])
            fetched += 1
            yield page
            if page.blocked or page.error or not page.html or depth >= settings.max_depth:
                continue
            for link, anchor in extract_links(page.html, page.final_url):
                norm = _normalize(link)
                if norm in seen or not norm.startswith(("http://", "https://")):
                    continue
                if not same_site(norm, seed) or _SKIP_EXT.search(urlsplit(norm).path):
                    continue
                score = link_score(norm, anchor, keywords, seed)
                if score <= 0:
                    continue
                seen.add(norm)
                origin[norm] = (page.final_url, anchor)
                order += 1
                heapq.heappush(frontier, (-score, depth + 1, order, norm))

    async def fetch_document(self, url: str) -> FetchedDocument:
        await self._polite(url)
        return await self._http.fetch_document(url)

    async def aclose(self) -> None:
        if self._page_fetcher is not self._http:
            await self._page_fetcher.aclose()
        await self._http.aclose()


class SiteCrawlerFactory:
    """One crawler (and one browser, if enabled) per campaign run."""

    def __init__(
        self,
        guard: SsrfGuard,
        *,
        use_playwright: bool,
        politeness_seconds: float,
        respect_robots: bool = True,
        slots: IBrowserSlots | None = None,
    ) -> None:
        self._guard = guard
        self._slots = slots
        self._use_playwright = use_playwright
        self._delay = politeness_seconds
        self._respect_robots = respect_robots

    def create(
        self, sessions: dict[str, str] | None = None, *, screenshots: bool = False
    ) -> SiteCrawler:
        """`sessions`: domain -> decrypted storage_state of a user-solved browser session.
        `screenshots`: capture text-sparse pages for the user's vision model."""
        http = SafeHttpFetcher(self._guard)
        page_fetcher: PageFetcher = (
            PlaywrightFetcher(self._guard, sessions, screenshots=screenshots, slots=self._slots)
            if self._use_playwright
            else http
        )
        return SiteCrawler(
            page_fetcher=page_fetcher,
            http=http,
            guard=self._guard,
            politeness_seconds=self._delay,
            respect_robots=self._respect_robots,
        )
