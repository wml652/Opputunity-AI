"""Page fetchers. Both re-validate EVERY URL they touch against the SSRF guard:

* `SafeHttpFetcher` follows redirects manually (max 5 hops), validating each `Location`.
* `PlaywrightFetcher` renders JavaScript in an isolated context and intercepts every request
  (`context.route("**/*")`): navigations, redirects, scripts, images, XHR. Anything that fails
  the guard is aborted; if the main document itself is refused, the page is reported `blocked`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from app.domain.discovery.models import FetchedPage
from app.domain.shared.errors import UnsafeUrlError
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

MAX_REDIRECTS = 5
MAX_BYTES = 3 * 1024 * 1024
HTML_TYPES = ("text/html", "application/xhtml+xml")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 OpportunityConsole/0.1"
)


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...

    async def aclose(self) -> None: ...


class SafeHttpFetcher:
    def __init__(
        self, guard: SsrfGuard, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._guard = guard
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "user-agent": USER_AGENT,
                "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            },
        )

    async def get_text(self, url: str) -> tuple[int | None, str, str]:
        """(status, final_url, text) for small text resources such as robots.txt."""
        page = await self.fetch(url, html_only=False)
        return page.status, page.final_url, page.html

    async def fetch(self, url: str, *, html_only: bool = True) -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                current = await self._guard.validate(current)
            except UnsafeUrlError as exc:
                return FetchedPage(
                    url=url,
                    final_url=current,
                    status=None,
                    html="",
                    blocked=True,
                    error=f"SSRF guard: {exc.message}",
                )
            try:
                async with self._client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return FetchedPage(
                                url,
                                current,
                                response.status_code,
                                "",
                                error="redirect without location",
                            )
                        current = urljoin(current, location)
                        continue
                    ctype = response.headers.get("content-type", "").lower()
                    if (
                        html_only
                        and response.status_code < 400
                        and not any(t in ctype for t in HTML_TYPES)
                    ):
                        return FetchedPage(
                            url,
                            current,
                            response.status_code,
                            "",
                            error=f"not HTML ({ctype or 'unknown'})",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    text = bytes(body).decode(response.encoding or "utf-8", "replace")
                    return FetchedPage(url, current, response.status_code, text)
            except httpx.HTTPError as exc:
                return FetchedPage(url, current, None, "", error=f"{type(exc).__name__}")
        return FetchedPage(url, current, None, "", error="too many redirects")

    async def aclose(self) -> None:
        await self._client.aclose()


class PlaywrightFetcher:
    """Headless Chromium. Requires a Proactor event loop on Windows (see runtime.event_loop)."""

    def __init__(self, guard: SsrfGuard, *, timeout_ms: int = 25_000) -> None:
        self._guard = guard
        self._timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
            return self._browser

    async def fetch(self, url: str) -> FetchedPage:
        try:
            first = await self._guard.validate(url)
        except UnsafeUrlError as exc:
            return FetchedPage(url, url, None, "", blocked=True, error=f"SSRF guard: {exc.message}")
        browser = await self._ensure()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            java_script_enabled=True,
            accept_downloads=False,
            service_workers="block",
        )
        blocked: list[str] = []

        async def guard_route(route: Any) -> None:
            request_url = route.request.url
            if request_url.startswith(("data:", "blob:", "about:")):
                await route.continue_()
                return
            try:
                await self._guard.validate(request_url)
            except UnsafeUrlError:
                blocked.append(request_url)
                await route.abort("blockedbyclient")
                return
            # Chromium follows redirects internally WITHOUT re-entering this handler, so the
            # request is performed here with redirects disabled and every Location is validated
            # before the browser is allowed to follow it (the next hop re-enters the handler).
            try:
                response = await route.fetch(max_redirects=0, timeout=self._timeout_ms)
            except Exception:  # noqa: BLE001 - network failure for this one request
                await route.abort("failed")
                return
            if 300 <= response.status < 400:
                location = response.headers.get("location")
                if location:
                    target = urljoin(request_url, location)
                    try:
                        await self._guard.validate(target)
                    except UnsafeUrlError:
                        blocked.append(target)
                        await route.abort("blockedbyclient")
                        return
            await route.fulfill(response=response)

        await context.route("**/*", guard_route)
        page = await context.new_page()
        try:
            response = await page.goto(
                first, wait_until="domcontentloaded", timeout=self._timeout_ms
            )
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=4_000)
            html = await page.content()
            final = page.url
            status = response.status if response else None
            if blocked and (final in blocked or final == "about:blank" or response is None):
                return FetchedPage(
                    url, final, None, "", blocked=True, error=f"SSRF guard blocked {blocked[0]}"
                )
            return FetchedPage(url, final, status, html, blocked_requests=len(blocked))
        except Exception as exc:  # noqa: BLE001 - navigation errors become a failed page
            if blocked:
                return FetchedPage(
                    url,
                    blocked[0],
                    None,
                    "",
                    blocked=True,
                    error=f"SSRF guard blocked {blocked[0]}",
                )
            return FetchedPage(url, url, None, "", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            await context.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._browser = self._pw = None


def playwright_available() -> bool:
    """True if the Playwright package and its Chromium build are installed."""
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    import os
    from pathlib import Path

    base = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData/Local/ms-playwright"
    )
    if not base.exists():
        base = Path.home() / ".cache/ms-playwright"
    # Headless mode uses the "chromium_headless_shell" build; Playwright writes the marker file
    # only after a download fully completes.
    return base.exists() and any(base.glob("chromium_headless_shell-*/INSTALLATION_COMPLETE"))
