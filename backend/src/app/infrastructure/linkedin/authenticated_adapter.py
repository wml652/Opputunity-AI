"""Opt-in authenticated LinkedIn job search (the user's own signed-in session).

Off by default. Enabled only after the user acknowledged LinkedIn's User Agreement and signed in
themselves in a visible browser (Settings > Search & Sessions); their cookies are stored
encrypted. Each search page counts against a strict daily action cap enforced by the caller.
Read-only: it never posts, messages, connects or applies. All traffic goes through the SSRF
egress proxy like every other browser."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import Any
from urllib.parse import urlencode

from app.domain.ports.discovery import Listing
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.fetchers import USER_AGENT
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

SEARCH_URL = "https://www.linkedin.com/jobs/search/"

CARDS_JS = r"""
() => [...document.querySelectorAll('li[data-occludable-job-id], .job-card-container, .base-card')]
  .slice(0, 50)
  .map((card) => {
    const link = card.querySelector('a.job-card-list__title, a.job-card-container__link, a.base-card__full-link, a[href*="/jobs/view/"]');
    const text = (sel) => (card.querySelector(sel)?.innerText || '').trim();
    return {
      title: (link?.innerText || text('.base-search-card__title')).trim().split('\n')[0],
      url: link ? link.href.split('?')[0] : '',
      organization: text('.artdeco-entity-lockup__subtitle, .base-search-card__subtitle, .job-card-container__primary-description'),
      location: text('.job-card-container__metadata-item, .job-search-card__location, .artdeco-entity-lockup__caption'),
      posted: card.querySelector('time')?.getAttribute('datetime') || '',
    };
  })
  .filter((c) => c.title && c.url)
"""


class LinkedInSessionExpiredError(Exception):
    """LinkedIn showed a login/auth wall: the stored session is no longer valid."""


class LinkedInAuthenticatedAdapter:
    def __init__(self, guard: SsrfGuard, *, pause_s: tuple[float, float] = (2.0, 4.0)) -> None:
        self._guard = guard
        self._pause = pause_s

    async def search(
        self, keyword: str, location: str | None, limit: int, storage_state: str
    ) -> list[Listing]:
        params = {"keywords": keyword, **({"location": location} if location else {})}
        url = f"{SEARCH_URL}?{urlencode(params)}"
        await self._guard.validate(url)
        from playwright.async_api import async_playwright

        proxy = EgressProxy(self._guard)
        port = await proxy.start()
        pw = await async_playwright().start()
        browser: Any = None
        try:
            browser = await pw.chromium.launch(
                headless=True,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--proxy-bypass-list=<-loopback>"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                storage_state=json.loads(storage_state),
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if any(marker in page.url for marker in ("/login", "/authwall", "/checkpoint")):
                raise LinkedInSessionExpiredError("LinkedIn asked to sign in again")
            await asyncio.sleep(random.uniform(*self._pause))  # noqa: S311 - pacing jitter
            raw: list[dict[str, str]] = await page.evaluate(CARDS_JS)
        finally:
            with contextlib.suppress(Exception):
                if browser is not None:
                    await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()
            await proxy.stop()
        listings: list[Listing] = []
        for card in raw[:limit]:
            listings.append(
                Listing(
                    title=card["title"][:300],
                    organization=(card.get("organization") or "")[:200] or None,
                    location=(card.get("location") or "")[:200] or None,
                    url=card["url"][:2000],
                    posted=card.get("posted") or None,
                    source="linkedin (signed in)",
                )
            )
        return listings
