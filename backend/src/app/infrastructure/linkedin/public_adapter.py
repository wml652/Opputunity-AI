"""LinkedIn public (logged-out) job listings via the guest search endpoint.

Read-only, no account, no cookies. Rate-limited with jitter between requests and a hard cap on
pages per keyword. Only listing cards are used (title, company, location, posted date, public
job URL); authenticated scraping / Easy Apply are out of scope here (opt-in, M5)."""

from __future__ import annotations

import asyncio
import random
import time

import httpx
from bs4 import BeautifulSoup

from app.domain.ports.discovery import Listing
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.fetchers import USER_AGENT

log = get_logger(__name__)

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeAdditionalJobPostings"
PAGE_SIZE = 25


class LinkedInPublicAdapter:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        min_interval: float = 2.5,
        jitter: float = 1.5,
        max_pages: int = 2,
    ) -> None:
        self._http = http
        self._min_interval = min_interval
        self._jitter = jitter
        self._max_pages = max_pages
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = (
                self._min_interval
                + random.uniform(0, self._jitter)  # noqa: S311 - politeness jitter only
                - (time.monotonic() - self._last)
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def search(self, keyword: str, location: str | None, limit: int) -> list[Listing]:
        listings: list[Listing] = []
        for page in range(self._max_pages):
            if len(listings) >= limit:
                break
            await self._throttle()
            params: dict[str, str | int] = {"keywords": keyword, "start": page * PAGE_SIZE}
            if location:
                params["location"] = location
            try:
                r = await self._http.get(
                    GUEST_SEARCH, params=params, headers={"user-agent": USER_AGENT}, timeout=20
                )
            except httpx.HTTPError as exc:
                log.warning("linkedin.request_failed", error=type(exc).__name__)
                break
            if r.status_code == 429:
                log.warning("linkedin.rate_limited")
                break
            if r.status_code != 200 or not r.text.strip():
                break
            batch = parse_job_cards(r.text)
            if not batch:
                break
            listings.extend(batch)
        return listings[:limit]


def parse_job_cards(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    for card in soup.select("li, div.base-card"):
        link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        title = card.select_one(".base-search-card__title, h3")
        if not link or not title or not link.get("href"):
            continue
        company = card.select_one(".base-search-card__subtitle, h4")
        location = card.select_one(".job-search-card__location")
        posted = card.select_one("time")
        url = str(link["href"]).split("?", 1)[0]
        if not url.startswith("https://") or "linkedin.com" not in url:
            continue
        out.append(
            Listing(
                title=" ".join(title.get_text(" ").split())[:300],
                organization=" ".join(company.get_text(" ").split())[:200] if company else None,
                location=" ".join(location.get_text(" ").split())[:200] if location else None,
                url=url,
                posted=(str(posted.get("datetime")) if posted and posted.get("datetime") else None),
                source="linkedin_public",
            )
        )
    unique = {item.url: item for item in out}
    return list(unique.values())
