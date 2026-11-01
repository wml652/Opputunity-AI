"""Page fetchers with flight-recorder traces. Every URL either fetcher touches passes the SSRF
guard:

* `SafeHttpFetcher` follows redirects manually (max 5 hops) and validates each `Location`.
* `PlaywrightFetcher` runs real Chromium behind `EgressProxy`, which enforces the guard at
  connection level for navigations, redirect hops, subresources, and loopback targets. A
  `context.route` check also aborts unsafe URLs early. Requests are made by the browser itself,
  so sites see a genuine browser (no Node-side `route.fetch`, which bot protection blocks).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain.discovery.models import FetchedPage
from app.domain.discovery.playbook import (
    FetchStrategy,
    Stage,
    StepStatus,
    Trace,
    detect_bot_challenge,
)
from app.domain.ports.discovery import FetchedDocument
from app.domain.ports.system import IBrowserSlots
from app.domain.shared.errors import DnsResolutionError, UnsafeUrlError
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

MAX_REDIRECTS = 5
MAX_BYTES = 3 * 1024 * 1024
HTML_TYPES = ("text/html", "application/xhtml+xml")
HTTP_FALLBACK_MIN_BYTES = 2_000
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
SPARSE_TEXT_CHARS = 1_500  # below this visible text, a page may be image/table-only
SCREENSHOT_MAX_HEIGHT = 6_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "user-agent": USER_AGENT,
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}


class PageFetcher(Protocol):
    async def fetch(self, url: str, strategy: FetchStrategy | None = None) -> FetchedPage: ...

    async def aclose(self) -> None: ...


async def _preflight(guard: SsrfGuard, url: str, trace: Trace) -> tuple[str, bool] | None:
    """Validate + resolve. Returns `(error, is_ssrf_block)` on failure. A host that does not
    resolve is refused too, but reported as `dns_error` (transient), never as an SSRF block."""
    with trace.step(Stage.PREFLIGHT_AND_SSRF, url=url) as step:
        try:
            normalized = await guard.validate(url)
            parts = urlsplit(normalized)
            step.detail["resolved_ips"] = await guard.resolve_pinned(
                parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
            )
        except DnsResolutionError as exc:
            step.status = StepStatus.FAIL
            step.detail["error"] = f"dns_error: {exc.message}"
            return f"dns_error: {exc.message}", False
        except UnsafeUrlError as exc:
            step.status = StepStatus.FAIL
            step.detail["error"] = f"SSRF guard: {exc.message}"
            return f"SSRF guard: {exc.message}", True
    return None


def _challenge_error(status: int | None, html: str, headers: dict[str, str]) -> str | None:
    vendor = detect_bot_challenge(status, html, headers)
    return f"bot_challenge: {vendor} interstitial (automation cannot pass it)" if vendor else None


class SafeHttpFetcher:
    def __init__(
        self, guard: SsrfGuard, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self._guard = guard
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers=BROWSER_HEADERS,
            http2=False,
        )

    async def get_text(self, url: str) -> tuple[int | None, str, str]:
        page = await self.fetch(url, html_only=False)
        return page.status, page.final_url, page.html

    async def fetch(
        self, url: str, strategy: FetchStrategy | None = None, *, html_only: bool = True
    ) -> FetchedPage:
        strategy = strategy or FetchStrategy(mode="http")
        trace = Trace()
        if strategy.backoff_s:
            await asyncio.sleep(strategy.backoff_s)
        refused = await _preflight(self._guard, url, trace)
        if refused:
            return FetchedPage(
                url,
                url,
                None,
                "",
                blocked=refused[1],
                error=refused[0],
                trace=trace.steps,
                strategy=strategy.to_dict(),
            )
        current = url
        hops: list[dict[str, Any]] = []
        with trace.step(Stage.HTTP_FETCH, strategy=strategy.to_dict()) as step:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    current = await self._guard.validate(current)
                except DnsResolutionError as exc:
                    step.status = StepStatus.FAIL
                    step.detail |= {"redirect_hops": hops, "error": f"dns_error: {exc.message}"}
                    return FetchedPage(
                        url,
                        current,
                        None,
                        "",
                        error=f"dns_error: {exc.message}",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
                except UnsafeUrlError as exc:
                    step.status = StepStatus.FAIL
                    step.detail |= {"redirect_hops": hops, "error": f"SSRF guard: {exc.message}"}
                    return FetchedPage(
                        url,
                        current,
                        None,
                        "",
                        blocked=True,
                        error=f"SSRF guard: {exc.message}",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
                try:
                    async with self._client.stream(
                        "GET", current, timeout=strategy.timeout_ms / 1000
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            hops.append(
                                {"from": current, "status": response.status_code, "to": location}
                            )
                            if not location:
                                break
                            current = urljoin(current, location)
                            continue
                        ctype = response.headers.get("content-type", "").lower()
                        step.detail |= {
                            "http_status": response.status_code,
                            "content_type": ctype,
                            "redirect_hops": hops,
                            "final_url": current,
                        }
                        if (
                            html_only
                            and response.status_code < 400
                            and not any(t in ctype for t in HTML_TYPES)
                        ):
                            step.status = StepStatus.FAIL
                            step.detail["error"] = f"not HTML ({ctype or 'unknown'})"
                            return FetchedPage(
                                url,
                                current,
                                response.status_code,
                                "",
                                error=f"not HTML ({ctype or 'unknown'})",
                                trace=trace.steps,
                                strategy=strategy.to_dict(),
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_BYTES:
                                break
                        text = bytes(body).decode(response.encoding or "utf-8", "replace")
                        step.detail["html_bytes"] = len(body)
                        challenge = _challenge_error(
                            response.status_code, text, dict(response.headers)
                        )
                        if challenge:
                            step.detail["error"] = challenge
                        if response.status_code >= 400 or challenge:
                            step.status = StepStatus.FAIL
                        return FetchedPage(
                            url,
                            current,
                            response.status_code,
                            text,
                            error=challenge,
                            trace=trace.steps,
                            strategy=strategy.to_dict(),
                        )
                except httpx.TimeoutException:
                    step.status = StepStatus.FAIL
                    step.detail |= {"redirect_hops": hops, "error": "timeout"}
                    return FetchedPage(
                        url,
                        current,
                        None,
                        "",
                        error="timeout",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
                except httpx.HTTPError as exc:
                    step.status = StepStatus.FAIL
                    step.detail |= {"redirect_hops": hops, "error": type(exc).__name__}
                    return FetchedPage(
                        url,
                        current,
                        None,
                        "",
                        error=type(exc).__name__,
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
            step.status = StepStatus.FAIL
            step.detail |= {"redirect_hops": hops, "error": "too many redirects"}
        return FetchedPage(
            url,
            current,
            None,
            "",
            error="too many redirects",
            trace=trace.steps,
            strategy=strategy.to_dict(),
        )

    async def fetch_document(self, url: str) -> FetchedDocument:
        """Binary download (PDF calls / brochures): SSRF-guarded on every redirect hop,
        capped at MAX_DOCUMENT_BYTES."""
        trace = Trace()
        refused = await _preflight(self._guard, url, trace)
        if refused:
            return FetchedDocument(url, url, None, "", b"", refused[0], trace.steps)
        current = url
        with trace.step(Stage.HTTP_FETCH, document=True) as step:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    current = await self._guard.validate(current)
                except UnsafeUrlError as exc:
                    step.status = StepStatus.FAIL
                    step.detail["error"] = f"SSRF guard: {exc.message}"
                    return FetchedDocument(
                        url, current, None, "", b"", f"SSRF guard: {exc.message}", trace.steps
                    )
                try:
                    async with self._client.stream("GET", current, timeout=45.0) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                break
                            current = urljoin(current, location)
                            continue
                        ctype = response.headers.get("content-type", "").split(";")[0].lower()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_DOCUMENT_BYTES:
                                step.status = StepStatus.FAIL
                                step.detail["error"] = "document too large"
                                return FetchedDocument(
                                    url,
                                    current,
                                    response.status_code,
                                    ctype,
                                    b"",
                                    "document too large",
                                    trace.steps,
                                )
                        step.detail |= {
                            "http_status": response.status_code,
                            "content_type": ctype,
                            "bytes": len(body),
                        }
                        if response.status_code >= 400:
                            step.status = StepStatus.FAIL
                        return FetchedDocument(
                            url,
                            current,
                            response.status_code,
                            ctype,
                            bytes(body),
                            None,
                            trace.steps,
                        )
                except httpx.HTTPError as exc:
                    step.status = StepStatus.FAIL
                    step.detail["error"] = type(exc).__name__
                    return FetchedDocument(
                        url, current, None, "", b"", f"network: {type(exc).__name__}", trace.steps
                    )
            step.status = StepStatus.FAIL
        return FetchedDocument(url, current, None, "", b"", "too many redirects", trace.steps)

    async def aclose(self) -> None:
        await self._client.aclose()


class PlaywrightFetcher:
    """Real headless Chromium (Proactor loop on Windows) behind the SSRF egress proxy."""

    def __init__(
        self,
        guard: SsrfGuard,
        sessions: dict[str, str] | None = None,
        *,
        screenshots: bool = False,
        slots: IBrowserSlots | None = None,
    ) -> None:
        self._guard = guard
        self._screenshots = screenshots  # only when the user configured a vision model
        self._slots = slots  # global browser pool: the slot is held while the browser lives
        self._slot: contextlib.AbstractAsyncContextManager[None] | None = None
        self._proxy = EgressProxy(guard)
        # domain -> storage_state of a browser session the user solved headfully (decrypted)
        self._sessions: dict[str, Any] = {k: json.loads(v) for k, v in (sessions or {}).items()}
        self._pw: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                if self._slots is not None and self._slot is None:
                    self._slot = self._slots.slot("auto")
                    await self._slot.__aenter__()
                port = await self._proxy.start()
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                    args=[
                        "--proxy-bypass-list=<-loopback>",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            return self._browser

    async def fetch(self, url: str, strategy: FetchStrategy | None = None) -> FetchedPage:
        strategy = strategy or FetchStrategy()
        if strategy.mode == "http":
            http = SafeHttpFetcher(self._guard)
            try:
                return await http.fetch(url, strategy)
            finally:
                await http.aclose()
        trace = Trace()
        if strategy.backoff_s:
            await asyncio.sleep(strategy.backoff_s)
        refused = await _preflight(self._guard, url, trace)
        if refused:
            return FetchedPage(
                url,
                url,
                None,
                "",
                blocked=refused[1],
                error=refused[0],
                trace=trace.steps,
                strategy=strategy.to_dict(),
            )
        browser = await self._ensure()
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        storage_state = self._sessions.get(host)
        if storage_state is not None:
            trace.add(Stage.BROWSER_FETCH, StepStatus.OK, session="user-solved browser session")
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            java_script_enabled=True,
            accept_downloads=False,
            service_workers="block",
            extra_http_headers={"accept-language": "en-US,en;q=0.9"},
            storage_state=storage_state,
        )
        route_blocked: list[str] = []

        async def guard_route(route: Any) -> None:
            request_url = route.request.url
            if not request_url.startswith(("data:", "blob:", "about:")):
                try:
                    await self._guard.validate(request_url)
                except UnsafeUrlError:
                    route_blocked.append(request_url)
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await context.route("**/*", guard_route)
        proxy_mark = len(self._proxy.blocked)
        error_mark = len(self._proxy.errors)
        kind = ""
        failed: FetchedPage | None = None
        page = await context.new_page()
        with trace.step(Stage.BROWSER_FETCH, strategy=strategy.to_dict()) as step:
            try:
                response = await page.goto(
                    url, wait_until=strategy.wait_until, timeout=strategy.timeout_ms
                )
                if strategy.hydration_ms:
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("networkidle", timeout=strategy.hydration_ms)
                html = await page.content()
                final = page.url
                status = response.status if response else None
                blocked = route_blocked + self._proxy.blocked[proxy_mark:]
                step.detail |= {
                    "http_status": status,
                    "final_url": final,
                    "html_bytes": len(html.encode()),
                    "blocked_requests": blocked[:10],
                    "wait_until": strategy.wait_until,
                }
                blocked_hosts = {urlsplit(b).hostname for b in blocked}
                denied = response is not None and response.headers.get("x-ootg-blocked") is not None
                if blocked and (
                    response is None or denied or urlsplit(final).hostname in blocked_hosts
                ):
                    step.status = StepStatus.FAIL
                    step.detail["error"] = f"SSRF guard blocked {blocked[0]}"
                    return FetchedPage(
                        url,
                        final,
                        None,
                        "",
                        blocked=True,
                        error=f"SSRF guard blocked {blocked[0]}",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                        blocked_requests=len(blocked),
                    )
                screenshot = await self._sparse_screenshot(page)
                gateway = response.headers.get("x-ootg-error") if response is not None else None
                if gateway:
                    step.status = StepStatus.FAIL
                    step.detail["error"] = f"{gateway}: upstream unreachable"
                    return FetchedPage(
                        url,
                        final,
                        None,
                        "",
                        error=f"{gateway}: upstream unreachable",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
                challenge = _challenge_error(
                    status, html, dict(response.headers) if response is not None else {}
                )
                if challenge:
                    step.detail["error"] = challenge
                if (status is not None and status >= 400) or challenge:
                    step.status = StepStatus.FAIL
                return FetchedPage(
                    url,
                    final,
                    status,
                    html,
                    error=challenge,
                    blocked_requests=len(blocked),
                    trace=trace.steps,
                    strategy=strategy.to_dict(),
                    screenshot=screenshot,
                )
            except Exception as exc:  # noqa: BLE001 - navigation errors become a typed failed page
                blocked = route_blocked + self._proxy.blocked[proxy_mark:]
                step.status = StepStatus.FAIL
                message = str(exc).split("\n", 1)[0][:200]
                step.detail |= {
                    "error": f"{type(exc).__name__}: {message}",
                    "blocked_requests": blocked[:10],
                }
                gateway_errors = self._proxy.errors[error_mark:]
                if gateway_errors and not blocked:
                    return FetchedPage(
                        url,
                        url,
                        None,
                        "",
                        error=f"{gateway_errors[-1][1]}: {message}",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                    )
                if blocked and (
                    "ERR_TUNNEL" in message
                    or "ERR_BLOCKED" in message
                    or "ERR_ABORTED" in message
                    or "ERR_FAILED" in message
                ):
                    return FetchedPage(
                        url,
                        blocked[0],
                        None,
                        "",
                        blocked=True,
                        error=f"SSRF guard blocked {blocked[0]}",
                        trace=trace.steps,
                        strategy=strategy.to_dict(),
                        blocked_requests=len(blocked),
                    )
                kind = (
                    "timeout"
                    if "Timeout" in type(exc).__name__ or "Timeout" in message
                    else type(exc).__name__
                )
                failed = FetchedPage(
                    url,
                    url,
                    None,
                    "",
                    error=f"{kind}: {message}",
                    trace=trace.steps,
                    strategy=strategy.to_dict(),
                )
            finally:
                with contextlib.suppress(Exception):
                    await context.close()
        assert failed is not None
        if kind == "timeout":
            return await self._http_fallback(url, strategy, trace, failed)
        return failed

    async def _sparse_screenshot(self, page: Any) -> bytes | None:
        """A JPEG of the rendered page when it carries little text (tables/posters as images),
        so the user's vision model can read it. Never taken without a vision model."""
        if not self._screenshots:
            return None
        try:
            visible = await page.evaluate("document.body ? document.body.innerText.length : 0")
            if int(visible) >= SPARSE_TEXT_CHARS:
                return None
            height = await page.evaluate("document.documentElement.scrollHeight")
            shot: bytes = await page.screenshot(
                type="jpeg",
                quality=70,
                full_page=int(height) <= SCREENSHOT_MAX_HEIGHT,
            )
            return shot
        except Exception:  # noqa: BLE001 - a missing screenshot only disables vision parsing
            return None

    async def _http_fallback(
        self, url: str, strategy: FetchStrategy, trace: Trace, failed: FetchedPage
    ) -> FetchedPage:
        """Browser navigation timed out: many slow sites are static HTML whose subresources
        stall `domcontentloaded`. Try a plain HTTP fetch right away before declaring failure."""
        http = SafeHttpFetcher(self._guard)
        try:
            alt = await http.fetch(url, replace(strategy, mode="http", timeout_ms=20_000))
        finally:
            await http.aclose()
        trace.extend(alt.trace)
        if (
            alt.error is None
            and alt.status is not None
            and alt.status < 400
            and len(alt.html) >= HTTP_FALLBACK_MIN_BYTES
        ):
            return FetchedPage(
                url,
                alt.final_url,
                alt.status,
                alt.html,
                trace=trace.steps,
                strategy=replace(strategy, mode="http").to_dict(),
            )
        return replace(failed, trace=trace.steps)

    async def aclose(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        await self._proxy.stop()
        self._browser = self._pw = None
        slot, self._slot = self._slot, None
        if slot is not None:
            await slot.__aexit__(None, None, None)


def playwright_available() -> bool:
    """True if Playwright and its fully downloaded headless Chromium build are installed."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    base = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData/Local/ms-playwright"
    )
    if not base.exists():
        base = Path.home() / ".cache/ms-playwright"
    return base.exists() and any(base.glob("chromium_headless_shell-*/INSTALLATION_COMPLETE"))
