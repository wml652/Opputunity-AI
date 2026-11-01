"""Remote viewer for a headless Chromium context: the user sees and drives it from the console.

* Video: CDP `Page.startScreencast` on the active tab (JPEG, only when the page changes; each
  frame is acknowledged) -> RedisRemoteBrowserHub.publish_frame. New tabs / popups become the
  active tab; when it closes, the previous open tab is shown again.
* Input: events from the console (mouse, keys, pasted text, back / forward / reload) replayed
  with Playwright's mouse and keyboard at viewport coordinates. "close" closes every tab (the
  session then ends the same way as closing a local window); "finish" calls `on_finish`.

Events are applied, never stored or logged - a user may be typing a portal password."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Callable
from typing import Any

from app.domain.ports.hitl import VIEWPORT, HitlSession
from app.infrastructure.browser.remote_hub import RedisRemoteBrowserHub
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

JPEG_QUALITY = 60
MAX_TEXT = 4_000
MAX_KEY = 32
BUTTONS = ("left", "right", "middle")
TOUCH_EVERY_S = 60.0


def _num(value: Any, high: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(high, n))


def _delta(value: Any) -> float:
    """Scroll delta, clamped to one screen-ish per event."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-4_000.0, min(4_000.0, n))


class RemoteView:
    def __init__(
        self,
        hub: RedisRemoteBrowserHub,
        session: HitlSession,
        *,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self._hub = hub
        self._session = session
        self._on_finish = on_finish
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._switch_lock = asyncio.Lock()

    async def attach(self, context: Any, page: Any) -> None:
        self._context = context
        context.on("page", lambda p: self._spawn(self._show(p)))
        await self._show(page)
        self._tasks.append(asyncio.create_task(self._input_loop()))
        self._tasks.append(asyncio.create_task(self._keepalive()))

    async def detach(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        await self._stop_screencast()

    def _spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.ensure_future(coro))

    # -- video ------------------------------------------------------------------------------------

    async def _show(self, page: Any) -> None:
        async with self._switch_lock:
            await self._stop_screencast()
            self._page = page
            page.on("close", lambda _p: self._spawn(self._fallback()))
            with contextlib.suppress(Exception):
                await self._publish(await page.screenshot(type="jpeg", quality=JPEG_QUALITY))
            try:
                cdp = await self._context.new_cdp_session(page)
                cdp.on("Page.screencastFrame", lambda params: self._spawn(self._frame(cdp, params)))
                await cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": JPEG_QUALITY,
                        "maxWidth": VIEWPORT[0],
                        "maxHeight": VIEWPORT[1],
                        "everyNthFrame": 1,
                    },
                )
                self._cdp = cdp
            except Exception as exc:  # noqa: BLE001 - a tab closing mid-switch
                log.info("remote_view.screencast_failed", error=type(exc).__name__)

    async def _fallback(self) -> None:
        if self._context is None:
            return
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if open_pages and (self._page is None or self._page.is_closed()):
            await self._show(open_pages[-1])

    async def _stop_screencast(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp is None:
            return
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        with contextlib.suppress(Exception):
            await cdp.detach()

    async def _frame(self, cdp: Any, params: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
        if cdp is not self._cdp:
            return  # a late frame from the previous tab
        with contextlib.suppress(Exception):
            await self._publish(base64.b64decode(params.get("data") or ""))

    async def _publish(self, jpeg: bytes) -> None:
        if jpeg:
            await self._hub.publish_frame(self._session.id, jpeg)

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(TOUCH_EVERY_S)
            with contextlib.suppress(Exception):
                await self._hub.touch(self._session)

    # -- input ------------------------------------------------------------------------------------

    async def _input_loop(self) -> None:
        async with self._hub.inputs(self._session.id) as events:
            while True:
                event = await events.get(timeout=1.0)
                if event is None:
                    continue
                try:
                    await self.apply(event)
                except Exception as exc:  # noqa: BLE001 - one bad event never ends the session
                    log.info("remote_view.input_failed", kind=str(event.get("type"))[:16],
                             error=type(exc).__name__)  # fmt: skip

    async def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "finish":
            if self._on_finish is not None:
                self._on_finish()
            return
        if kind == "close":
            for p in list(self._context.pages if self._context else []):
                with contextlib.suppress(Exception):
                    await p.close()
            return
        page = self._page
        if page is None or page.is_closed():
            return
        if kind == "mouse":
            x, y = _num(event.get("x"), VIEWPORT[0]), _num(event.get("y"), VIEWPORT[1])
            button = event.get("button") if event.get("button") in BUTTONS else "left"
            action = event.get("action")
            if action == "move":
                await page.mouse.move(x, y)
            elif action == "down":
                await page.mouse.move(x, y)
                await page.mouse.down(button=button)
            elif action == "up":
                await page.mouse.move(x, y)
                await page.mouse.up(button=button)
            elif action == "click":
                await page.mouse.click(x, y, button=button)
            elif action == "wheel":
                await page.mouse.move(x, y)
                await page.mouse.wheel(_delta(event.get("dx")), _delta(event.get("dy")))
        elif kind == "key":
            key = str(event.get("key") or "")[:MAX_KEY]
            if not key:
                return
            if event.get("action") == "up":
                await page.keyboard.up(key)
            else:
                await page.keyboard.down(key)
        elif kind == "text":
            text = str(event.get("text") or "")[:MAX_TEXT]
            if text:
                await page.keyboard.insert_text(text)
        elif kind == "nav":
            action = event.get("action")
            if action == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=30_000)
            elif action == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=30_000)
            elif action == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
