"""Headful session capture: the USER passes a bot challenge or signs in, we keep the cookies.

A visible Chromium window opens on the user's own machine (local runtime only), behind the same
SSRF egress proxy as every other browser. We poll until the page is usable - no challenge
interstitial, real content (or, for LinkedIn, a signed-in session) - and return the context's
`storage_state`. Nothing here clicks, types or solves anything."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from app.domain.discovery.playbook import detect_bot_challenge
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.fetchers import USER_AGENT
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

POLL_S = 2.0
MIN_USABLE_HTML = 3_000


class SessionCaptureError(Exception):
    pass


class PlaywrightSessionCapturer:
    def __init__(self, guard: SsrfGuard) -> None:
        self._guard = guard

    async def capture(self, url: str, *, timeout_s: float, wait_for_close: bool = False) -> str:
        """`wait_for_close`: portal sign-ins - keep the window open until the USER closes it
        (signing in takes time); cookies are snapshotted while it is open."""
        await self._guard.validate(url)
        from playwright.async_api import async_playwright

        proxy = EgressProxy(self._guard)
        port = await proxy.start()
        pw = await async_playwright().start()
        browser: Any = None
        try:
            browser = await pw.chromium.launch(
                headless=False,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--proxy-bypass-list=<-loopback>"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT, locale="en-US", viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()
            with contextlib.suppress(Exception):
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + timeout_s
            linkedin = "linkedin.com" in url
            snapshot: str | None = None
            while time.monotonic() < deadline:
                if wait_for_close:
                    open_pages = [p for p in context.pages if not p.is_closed()]
                    if not open_pages:
                        if snapshot is None:
                            raise SessionCaptureError(
                                "the window was closed before any page loaded"
                            )
                        return snapshot
                    with contextlib.suppress(Exception):
                        snapshot = json.dumps(await context.storage_state())
                    await asyncio.sleep(POLL_S)
                    continue
                if page.is_closed():
                    raise SessionCaptureError("the browser window was closed before it was usable")
                if await self._usable(page, context, linkedin):
                    # Give the site a moment to persist cookies after the challenge/login.
                    await asyncio.sleep(1.0)
                    return json.dumps(await context.storage_state())
                await asyncio.sleep(POLL_S)
            if wait_for_close and snapshot is not None:
                return snapshot
            raise SessionCaptureError(f"not completed within {int(timeout_s)} s")
        finally:
            with contextlib.suppress(Exception):
                if browser is not None:
                    await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()
            await proxy.stop()

    async def _usable(self, page: Any, context: Any, linkedin: bool) -> bool:
        try:
            if linkedin:
                cookies = await context.cookies("https://www.linkedin.com")
                return any(c.get("name") == "li_at" for c in cookies)
            html = await page.content()
        except Exception:  # noqa: BLE001 - mid-navigation; try again on the next poll
            return False
        return len(html) >= MIN_USABLE_HTML and detect_bot_challenge(200, html, {}) is None
