"""Teach Mode: a VISIBLE Chromium window in which the student applies (or signs in) themselves
while an injected DOM recorder reports every click, navigation, new tab, field value, dropdown
choice, checkbox and upload to the backend (Playwright `expose_binding`).

Passwords are never sent (`secret`, value dropped in the page). A floating toolbar offers
"Finish training & save to brain"; closing the window also finishes. Same SSRF egress proxy as
every other browser. Nothing here clicks or types on the user's behalf."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.domain.agent.models import StepKind, TeachStep
from app.domain.ports.agent import StepSink, TeachResult
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.fetchers import USER_AGENT
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

POLL_S = 1.0
STATE_EVERY_S = 5.0

RECORDER_JS = r"""
(() => {
  if (window.__ootgInstalled) return;
  window.__ootgInstalled = true;
  const send = (ev) => { try { window.__ootgRecordEvent(ev); } catch (e) {} };
  const labelOf = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const parent = el.closest('label'); if (parent) return parent.innerText.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map((i) => document.getElementById(i)?.innerText.trim() || '').join(' ').trim(); if (t) return t; }
    return el.getAttribute('aria-label') || el.placeholder || el.name || el.title || '';
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role'); if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(t)) return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      return 'textbox';
    }
    return '';
  };
  const cssOf = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
    const parts = []; let n = el;
    while (n && n.nodeType === 1 && parts.length < 4) {
      let s = n.tagName.toLowerCase();
      const same = [...(n.parentElement ? n.parentElement.children : [])].filter((x) => x.tagName === n.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      parts.unshift(s); n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const vis = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const pageState = () => {
    const buttons = [];
    for (const el of document.querySelectorAll('a[href],button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],input[type=button],summary')) {
      if (!vis(el) || el.closest('#__ootg_toolbar')) continue;
      const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
      if (t) buttons.push(t);
      if (buttons.length >= 60) break;
    }
    const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=password]),select,textarea')]
      .filter(vis).map((el) => labelOf(el)).filter(Boolean).slice(0, 40);
    const headings = [...document.querySelectorAll('h1,h2,h3')].filter(vis).map((h) => h.innerText.trim()).filter(Boolean).slice(0, 6);
    return { title: document.title, headings, buttons, inputs };
  };
  const loc = (el) => ({
    role: roleOf(el),
    name: (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 120),
    label: labelOf(el).slice(0, 200), placeholder: el.placeholder || '', css: cssOf(el),
    href: el.href || '',
  });
  document.addEventListener('click', (e) => {
    const el = e.target.closest('a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],input[type=button],summary');
    if (!el || el.closest('#__ootg_toolbar')) return;
    send({ type: 'click', url: location.href, locator: loc(el), dom: pageState() });
  }, true);
  const timers = new WeakMap();
  const field = (el) => {
    const tag = el.tagName.toLowerCase();
    if (!['input', 'select', 'textarea'].includes(tag)) return;
    const type = tag === 'input' ? (el.type || 'text').toLowerCase() : tag;
    if (['submit', 'button', 'hidden', 'reset', 'image'].includes(type)) return;
    const secret = type === 'password';
    let value = null;
    if (!secret) {
      if (type === 'checkbox') value = el.checked ? 'Yes' : 'No';
      else if (type === 'radio') { if (!el.checked) return; value = labelOf(el) || el.value; }
      else if (type === 'file') value = el.files && el.files[0] ? el.files[0].name : '';
      else if (tag === 'select') value = (el.options[el.selectedIndex] || {}).text || el.value;
      else value = el.value;
    }
    const kind = type === 'file' ? 'upload' : tag === 'select' ? 'select'
      : (type === 'checkbox' || type === 'radio') ? 'check' : 'fill';
    const legend = el.closest('fieldset') && el.closest('fieldset').querySelector('legend');
    const label = type === 'radio' ? ((legend && legend.innerText.trim()) || el.name) : labelOf(el);
    send({ type: kind, url: location.href, locator: loc(el), field_label: String(label).slice(0, 300),
           field_name: el.name || el.id || '', field_type: type,
           value: value === null ? null : String(value).trim().slice(0, 4000), secret,
           dom: pageState() });
  };
  document.addEventListener('change', (e) => field(e.target), true);
  document.addEventListener('input', (e) => {
    const el = e.target;
    clearTimeout(timers.get(el));
    timers.set(el, setTimeout(() => field(el), 700));  // typing: one event per pause
  }, true);
  document.addEventListener('submit', (e) => send({ type: 'submit', url: location.href, locator: loc(e.target) }), true);
  const toolbar = () => {
    if (!document.body || document.getElementById('__ootg_toolbar')) return;
    const bar = document.createElement('div');
    bar.id = '__ootg_toolbar';
    bar.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;background:#17171a;'
      + 'color:#fafafa;font:12px system-ui,sans-serif;padding:8px 10px;border-radius:4px;display:flex;'
      + 'gap:8px;align-items:center;box-shadow:0 2px 8px rgba(0,0,0,.3)';
    const label = document.createElement('span');
    label.textContent = 'Teach Mode: the agent is learning from your steps';
    const btn = document.createElement('button');
    btn.textContent = 'Finish training & save to brain';
    btn.style.cssText = 'background:#047857;color:#fff;border:0;border-radius:3px;padding:4px 8px;cursor:pointer;font:inherit';
    btn.addEventListener('click', (e) => {
      e.stopPropagation(); e.preventDefault();
      send({ type: 'finish', url: location.href });
      label.textContent = 'Saved to the agent brain - you can close this window';
    });
    bar.append(label, btn);
    document.body.appendChild(bar);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', toolbar);
  else toolbar();
})();
"""

Driver = Callable[[Any], Awaitable[None]]


class PlaywrightTeachRecorder:
    """`driver` (tests only) acts on the page in place of a human, then finishes."""

    def __init__(
        self, guard: SsrfGuard, *, headless: bool = False, driver: Driver | None = None
    ) -> None:
        self._guard = guard
        self._headless = headless
        self._driver = driver

    async def record(
        self,
        url: str,
        *,
        storage_state: str | None,
        on_step: StepSink,
        timeout_s: float,
        stop: asyncio.Event,
    ) -> TeachResult:
        await self._guard.validate(url)
        from playwright.async_api import async_playwright

        steps: list[TeachStep] = []
        visited: list[str] = []
        pending: set[asyncio.Future[None]] = set()
        last = {"url": url, "text": "", "state": storage_state}
        proxy = EgressProxy(self._guard)
        port = await proxy.start()
        pw = await async_playwright().start()
        browser: Any = None

        async def add(step: TeachStep | None) -> None:
            if step is None:
                return
            steps.append(step)
            with contextlib.suppress(Exception):  # a slow sink never breaks recording
                await on_step(step)

        async def binding(source: Any, event: dict[str, Any]) -> None:
            if not isinstance(event, dict):
                return
            if event.get("type") == "finish":
                stop.set()
                return
            await add(TeachStep.from_dict(event))

        def on_navigated(frame: Any) -> None:
            if frame.parent_frame is None and frame.url.startswith("http"):
                visited.append(frame.url)
                pending.add(asyncio.ensure_future(add(TeachStep(StepKind.NAVIGATE, frame.url))))

        def watch(page: Any, *, new_tab: bool) -> None:
            page.on("framenavigated", on_navigated)
            if new_tab:
                pending.add(asyncio.ensure_future(add(TeachStep(StepKind.OPEN_TAB, page.url))))

        try:
            browser = await pw.chromium.launch(
                headless=self._headless,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--proxy-bypass-list=<-loopback>"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1280, "height": 900},
                storage_state=json.loads(storage_state) if storage_state else None,
            )
            await context.expose_binding("__ootgRecordEvent", binding)
            await context.add_init_script(RECORDER_JS)
            context.on("page", lambda p: watch(p, new_tab=True))
            page = await context.new_page()
            watch(page, new_tab=False)
            with contextlib.suppress(Exception):
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if self._driver is not None:
                await self._driver(page)
                await asyncio.sleep(1.0)  # let debounced field events arrive
                stop.set()
            deadline = time.monotonic() + timeout_s
            state_at = 0.0
            while not stop.is_set() and time.monotonic() < deadline:
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    break  # the user closed the window: that finishes the session too
                active = pages[-1]
                with contextlib.suppress(Exception):
                    last["url"] = active.url
                    last["text"] = str(
                        await active.evaluate(
                            "() => (document.body?.innerText || '').slice(0, 3000)"
                        )
                    )
                if time.monotonic() - state_at >= STATE_EVERY_S:
                    state_at = time.monotonic()
                    with contextlib.suppress(Exception):
                        last["state"] = json.dumps(await context.storage_state())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), POLL_S)
            screenshot: bytes | None = None
            pages = [p for p in context.pages if not p.is_closed()]
            if pages:
                active = pages[-1]
                with contextlib.suppress(Exception):
                    last["url"] = active.url
                    last["text"] = str(
                        await active.evaluate(
                            "() => (document.body?.innerText || '').slice(0, 3000)"
                        )
                    )
                    screenshot = await active.screenshot(full_page=True, type="png")
                with contextlib.suppress(Exception):
                    last["state"] = json.dumps(await context.storage_state())
            return TeachResult(
                steps=steps,
                storage_state=last["state"],
                final_url=str(last["url"] or url),
                final_text=str(last["text"] or ""),
                screenshot_png=screenshot,
                visited_urls=visited,
            )
        finally:
            with contextlib.suppress(Exception):
                if browser is not None:
                    await browser.close()
            with contextlib.suppress(Exception):
                await pw.stop()
            await proxy.stop()
