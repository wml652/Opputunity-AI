"""Playwright form filler: fills a multi-step portal up to - never through - the final Submit.

Per step: scan visible controls (with their human labels) -> ask the application's resolver ->
fill everything resolved with confidence >= 0.85 -> if a required field is still unknown, take a
checkpoint (encrypted storage_state + DOM fingerprint + fill ledger + screenshot) and PAUSE;
otherwise click Next/Continue. On reaching a step whose only way forward is Submit: checkpoint
and report READY_TO_SUBMIT.

Resume: restore storage_state and open the checkpoint URL. If the step's DOM fingerprint still
matches, continue there; if the portal lost the draft (fingerprint differs / redirected to the
start), replay from step 0 - the resolver returns the settled values for every earlier field.
All browser traffic goes through the SSRF egress proxy.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.domain.applications.filling import (
    CheckpointData,
    DocumentBlob,
    FieldResolution,
    FillOutcome,
    FillOutcomeKind,
    FormField,
    ResumeState,
    UnresolvedField,
    dom_fingerprint,
)
from app.domain.applications.verification import (
    PreviewValue,
    SubmitOutcome,
    SubmitOutcomeKind,
    compute_preview_hash,
    normalize_value,
)
from app.domain.ports.applications import Resolver
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.fetchers import USER_AGENT
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

MAX_STEPS = 12
NAV_TIMEOUT_MS = 30_000
NEXT_RE = re.compile(r"\b(next|continue|proceed|save (and|&) continue|forward)\b", re.I)
SUBMIT_RE = re.compile(r"\b(submit|send application|apply now|finish)\b", re.I)
CAPTCHA_RE = re.compile(r"(g-recaptcha|hcaptcha|cf-turnstile|captcha)", re.I)
LOGIN_WALL_ERROR = (
    'the portal asks you to sign in; use "Sign In in Browser & Resume" - automation never '
    "types passwords"
)


async def _is_login_wall(page: Any) -> bool:
    """A visible password field means a sign-in form: never fill it, never ask for it."""
    count = await page.locator("input[type=password]:visible").count()
    return bool(count)


TRUTHY = {"yes", "true", "1", "on", "y", "agree", "i agree"}

SCAN_JS = r"""
() => {
  const labelFor = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const parent = el.closest('label'); if (parent) return parent.innerText.trim();
    return el.getAttribute('aria-label') || el.placeholder || el.name || '';
  };
  const visible = (el) => { const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null; };
  const out = []; const seen = new Set();
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    const key = el.name || el.id; if (!key || seen.has(key)) continue;
    if (type !== 'file' && !visible(el)) continue;
    seen.add(key);
    if (type === 'radio') {
      const group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(key)}"]`)];
      const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
      out.push({ key, type: 'radio', label: legend ? legend.innerText.trim() : key,
                 options: group.map((r) => labelFor(r) || r.value), required: group.some((r) => r.required),
                 maxLength: null, accept: null });
      continue;
    }
    out.push({ key, type: tag === 'select' ? 'select' : type, label: labelFor(el),
               options: tag === 'select' ? [...el.options].filter((o) => o.value !== '').map((o) => o.text.trim()) : [],
               required: !!el.required, maxLength: el.maxLength > 0 ? el.maxLength : null, accept: el.accept || null });
  }
  return out;
}
"""

BUTTONS_JS = r"""
() => [...document.querySelectorAll('button, input[type=submit]')]
  .filter((b) => b.offsetParent !== null && !b.disabled)
  .map((b, i) => { b.setAttribute('data-ootg-btn', String(i)); return { i, text: (b.innerText || b.value || '').trim() }; })
"""


class PlaywrightFormFiller:
    def __init__(self, guard: SsrfGuard, *, headless: bool = True) -> None:
        self._guard = guard
        self._headless = headless

    async def run(
        self,
        portal_url: str,
        *,
        resume: ResumeState | None,
        resolver: Resolver,
        documents: dict[uuid.UUID, DocumentBlob],
        storage_state: str | None = None,
    ) -> FillOutcome:
        await self._guard.validate(portal_url)
        initial_state = resume.storage_state if resume else storage_state
        from playwright.async_api import async_playwright

        proxy = EgressProxy(self._guard)
        port = await proxy.start()
        log_lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ootg-upload-") as tmp:
            paths = _write_documents(documents, Path(tmp))
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=self._headless,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--proxy-bypass-list=<-loopback>"],
            )
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                    accept_downloads=False,
                    storage_state=json.loads(initial_state) if initial_state else None,
                )
                page = await context.new_page()
                page.set_default_timeout(NAV_TIMEOUT_MS)
                return await self._drive(
                    page, context, portal_url, resume, resolver, paths, log_lines
                )
            finally:
                with contextlib.suppress(Exception):
                    await browser.close()
                with contextlib.suppress(Exception):
                    await pw.stop()
                await proxy.stop()
        # TemporaryDirectory removed the decrypted documents here.

    async def _drive(
        self,
        page: Any,
        context: Any,
        portal_url: str,
        resume: ResumeState | None,
        resolver: Resolver,
        paths: dict[uuid.UUID, str],
        log_lines: list[str],
    ) -> FillOutcome:
        step_index, replayed = 0, False
        ledger: list[dict[str, Any]] = []
        if resume is not None:
            await page.goto(resume.url, wait_until="domcontentloaded")
            fields = await _scan(page)
            if fields and dom_fingerprint(fields) == resume.dom_fingerprint:
                step_index = resume.step_index
                log_lines.append(f"resumed at step {step_index + 1} (checkpoint intact)")
            else:
                replayed = True
                log_lines.append(
                    "portal lost the draft; replaying from step 1 using the fill ledger"
                )
                await page.goto(portal_url, wait_until="domcontentloaded")
        else:
            await page.goto(portal_url, wait_until="domcontentloaded")

        completed = 0
        for _ in range(MAX_STEPS):
            html = await page.content()
            if CAPTCHA_RE.search(html):
                return await self._outcome(
                    FillOutcomeKind.BLOCKED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    error="the portal shows a CAPTCHA; a human must continue",
                )
            if await _is_login_wall(page):
                return await self._outcome(
                    FillOutcomeKind.BLOCKED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    error=LOGIN_WALL_ERROR,
                )
            fields = await _scan(page)
            buttons = await page.evaluate(BUTTONS_JS)
            if not fields and not buttons:
                return await self._outcome(
                    FillOutcomeKind.BLOCKED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    error="no application form found on the page",
                )
            resolutions = await resolver(step_index, fields)
            unresolved: list[UnresolvedField] = []
            title = await page.title()
            for f, res in zip(fields, resolutions, strict=True):
                if res.fillable:
                    try:
                        await _fill(page, f, res, paths)
                        ledger.append(
                            {"step": step_index, "field_key": f.key, "source": res.source.value}
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad control must not abort the run
                        log_lines.append(f"could not fill {f.key}: {type(exc).__name__}")
                        if f.required:
                            unresolved.append(
                                UnresolvedField(
                                    f, res, step_index, f"{title} - step {step_index + 1}"
                                )
                            )
                elif f.required:
                    unresolved.append(
                        UnresolvedField(f, res, step_index, f"{title} - step {step_index + 1}")
                    )
            if unresolved:
                log_lines.append(
                    f"paused at step {step_index + 1}: {len(unresolved)} unknown required field(s)"
                )
                return await self._outcome(
                    FillOutcomeKind.PAUSED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    fields=fields,
                    unresolved=unresolved,
                )
            next_btn = next((b for b in buttons if NEXT_RE.search(b["text"])), None)
            submit_btn = next((b for b in buttons if SUBMIT_RE.search(b["text"])), None)
            if next_btn is not None:
                with contextlib.suppress(Exception):
                    async with page.expect_navigation(
                        wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                    ):
                        await page.click(f'[data-ootg-btn="{next_btn["i"]}"]')
                step_index += 1
                completed += 1
                log_lines.append(f"completed step {step_index}")
                continue
            if submit_btn is not None:
                log_lines.append(
                    f"reached the final step ({step_index + 1}); stopping before '{submit_btn['text']}'"
                )
                return await self._outcome(
                    FillOutcomeKind.READY_TO_SUBMIT,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    fields=fields,
                )
            return await self._outcome(
                FillOutcomeKind.BLOCKED,
                page,
                context,
                step_index,
                ledger,
                replayed,
                completed,
                log_lines,
                error="no Next or Submit button on this step",
            )
        return await self._outcome(
            FillOutcomeKind.BLOCKED,
            page,
            context,
            step_index,
            ledger,
            replayed,
            completed,
            log_lines,
            error=f"more than {MAX_STEPS} steps; stopping for safety",
        )

    # -- final submission (only after a recorded human OK) ---------------------------------------

    async def submit(
        self,
        portal_url: str,
        *,
        approved: dict[str, FieldResolution],
        approved_hash: str,
        storage_state: str | None,
        documents: dict[uuid.UUID, DocumentBlob],
    ) -> SubmitOutcome:
        """Replay from step 1 with the APPROVED values (so edits to earlier steps are applied),
        read every control back from the page, and click Submit only if the hash of what the
        page shows equals `approved_hash`. Otherwise nothing is submitted."""
        await self._guard.validate(portal_url)
        from playwright.async_api import async_playwright

        proxy = EgressProxy(self._guard)
        port = await proxy.start()
        log_lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ootg-submit-") as tmp:
            paths = _write_documents(documents, Path(tmp))
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=self._headless,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--proxy-bypass-list=<-loopback>"],
            )
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                    accept_downloads=False,
                    storage_state=json.loads(storage_state) if storage_state else None,
                )
                page = await context.new_page()
                page.set_default_timeout(NAV_TIMEOUT_MS)
                return await self._submit_drive(
                    page, portal_url, approved, approved_hash, paths, log_lines
                )
            finally:
                with contextlib.suppress(Exception):
                    await browser.close()
                with contextlib.suppress(Exception):
                    await pw.stop()
                await proxy.stop()

    async def _submit_drive(
        self,
        page: Any,
        portal_url: str,
        approved: dict[str, FieldResolution],
        approved_hash: str,
        paths: dict[uuid.UUID, str],
        log_lines: list[str],
    ) -> SubmitOutcome:
        await page.goto(portal_url, wait_until="domcontentloaded")
        observed: list[PreviewValue] = []
        seen: set[str] = set()
        for step_index in range(MAX_STEPS):
            html = await page.content()
            if CAPTCHA_RE.search(html):
                return SubmitOutcome(
                    SubmitOutcomeKind.BLOCKED,
                    error="the portal shows a CAPTCHA; a human must submit",
                    log=log_lines,
                )
            if await _is_login_wall(page):
                return SubmitOutcome(
                    SubmitOutcomeKind.BLOCKED, error=LOGIN_WALL_ERROR, log=log_lines
                )
            fields = await _scan(page)
            buttons = await page.evaluate(BUTTONS_JS)
            mismatches: list[dict[str, str]] = []
            for f in fields:
                res = approved.get(f.key)
                if res is None or res.value is None:
                    if f.required:
                        mismatches.append(
                            {"field": f.key, "expected": "(no approved value)", "observed": ""}
                        )
                        continue
                    # Optional and left blank in the approved preview: part of the hash, too.
                    shown_blank = await _read_back(page, f, "")
                    if normalize_value(shown_blank):
                        mismatches.append({"field": f.key, "expected": "", "observed": shown_blank})
                        continue
                    observed.append(PreviewValue(step_index, f.key, None))
                    continue
                try:
                    await _fill(page, f, res, paths)
                except Exception as exc:  # noqa: BLE001 - reported as a mismatch, never submitted
                    mismatches.append(
                        {"field": f.key, "expected": res.value, "observed": type(exc).__name__}
                    )
                    continue
                shown = await _read_back(page, f, res.value)
                if normalize_value(shown) != normalize_value(res.value):
                    mismatches.append({"field": f.key, "expected": res.value, "observed": shown})
                    continue
                observed.append(PreviewValue(step_index, f.key, res.value))
                seen.add(f.key)
            if mismatches:
                log_lines.append(f"step {step_index + 1}: {len(mismatches)} value(s) differ")
                return SubmitOutcome(
                    SubmitOutcomeKind.MISMATCH,
                    mismatches=mismatches,
                    error="the page does not show the approved values",
                    log=log_lines,
                )
            next_btn = next((b for b in buttons if NEXT_RE.search(b["text"])), None)
            submit_btn = next((b for b in buttons if SUBMIT_RE.search(b["text"])), None)
            if next_btn is not None:
                with contextlib.suppress(Exception):
                    async with page.expect_navigation(
                        wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                    ):
                        await page.click(f'[data-ootg-btn="{next_btn["i"]}"]')
                log_lines.append(f"step {step_index + 1} verified")
                continue
            if submit_btn is None:
                return SubmitOutcome(
                    SubmitOutcomeKind.BLOCKED,
                    error="no Next or Submit button on this step",
                    log=log_lines,
                )
            missing = sorted(set(approved) - seen)
            if missing:
                return SubmitOutcome(
                    SubmitOutcomeKind.MISMATCH,
                    mismatches=[
                        {
                            "field": k,
                            "expected": approved[k].value or "",
                            "observed": "(not on page)",
                        }
                        for k in missing
                    ],
                    error="approved fields were not found on the portal",
                    log=log_lines,
                )
            observed_hash = compute_preview_hash(
                PreviewValue(v.step_index, v.field_key, v.value) for v in observed
            )
            # Steps may be re-numbered by the portal; compare on (key, value) like the approval.
            if observed_hash != approved_hash and _hash_by_key(observed) != _hash_by_key_map(
                approved
            ):
                return SubmitOutcome(
                    SubmitOutcomeKind.MISMATCH,
                    observed_hash=observed_hash,
                    error="observed values do not match the approved preview hash",
                    log=log_lines,
                )
            log_lines.append(
                f"all {len(observed)} values verified; clicking '{submit_btn['text']}'"
            )
            with contextlib.suppress(Exception):
                async with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                ):
                    await page.click(f'[data-ootg-btn="{submit_btn["i"]}"]')
            receipt_png: bytes | None = None
            with contextlib.suppress(Exception):
                receipt_png = await page.screenshot(full_page=True, type="png")
            text = ""
            with contextlib.suppress(Exception):
                text = " ".join((await page.inner_text("body")).split())[:4000]
            return SubmitOutcome(
                SubmitOutcomeKind.SUBMITTED,
                observed_hash=observed_hash,
                receipt_png=receipt_png,
                receipt_text=text,
                final_url=page.url,
                log=log_lines,
            )
        return SubmitOutcome(
            SubmitOutcomeKind.BLOCKED, error=f"more than {MAX_STEPS} steps", log=log_lines
        )

    async def _outcome(
        self,
        kind: FillOutcomeKind,
        page: Any,
        context: Any,
        step_index: int,
        ledger: list[dict[str, Any]],
        replayed: bool,
        completed: int,
        log_lines: list[str],
        *,
        fields: list[FormField] | None = None,
        unresolved: list[UnresolvedField] | None = None,
        error: str | None = None,
    ) -> FillOutcome:
        if fields is None:
            with contextlib.suppress(Exception):
                fields = await _scan(page)
        screenshot = None
        with contextlib.suppress(Exception):
            screenshot = await page.screenshot(full_page=True, type="png")
        state = json.dumps(await context.storage_state())
        checkpoint = CheckpointData(
            step_index=step_index,
            url=page.url,
            dom_fingerprint=dom_fingerprint(fields or []),
            storage_state=state,
            screenshot_png=screenshot,
            fill_ledger=ledger,
        )
        return FillOutcome(
            kind=kind,
            step_index=step_index,
            checkpoint=checkpoint,
            unresolved=unresolved or [],
            replayed=replayed,
            steps_completed=completed,
            error=error,
            log=log_lines,
        )


async def _scan(page: Any) -> list[FormField]:
    raw = await page.evaluate(SCAN_JS)
    return [
        FormField(
            key=str(r["key"]),
            label=str(r["label"] or r["key"])[:500],
            field_type=str(r["type"]),
            selector=f'[name="{r["key"]}"]',
            options=[str(o) for o in r.get("options") or []],
            required=bool(r.get("required")),
            max_length=r.get("maxLength"),
            accept=r.get("accept"),
        )
        for r in raw
    ]


READ_JS = r"""
([name, type]) => {
  const els = [...document.querySelectorAll(`[name="${CSS.escape(name)}"]`)];
  const el = els[0];
  if (!el) return null;
  if (type === 'select') { const o = el.options[el.selectedIndex]; return o ? JSON.stringify([o.text.trim(), o.value]) : ''; }
  if (type === 'radio') {
    const r = els.find((x) => x.checked); if (!r) return '';
    const label = (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)?.innerText.trim()) || r.closest('label')?.innerText.trim() || r.value;
    return JSON.stringify([label, r.value]);
  }
  if (type === 'checkbox') return el.checked ? 'on' : '';
  if (type === 'file') return el.files && el.files.length ? el.files[0].name : '';
  return el.value;
}
"""


async def _read_back(page: Any, f: FormField, expected: str) -> str:
    """What the page REALLY shows for this control, mapped onto the approved representation
    (select/radio: the option label or value; checkbox: truthiness; file: a file is attached)."""
    raw = await page.evaluate(READ_JS, [f.key, f.field_type])
    if raw is None:
        return "(missing)"
    if f.field_type in ("select", "radio"):
        if not raw:
            return ""
        label, value = json.loads(raw)
        wanted = normalize_value(expected).lower()
        return expected if wanted in (normalize_value(label).lower(), value.lower()) else label
    if f.field_type == "checkbox":
        want_on = expected.strip().lower() in TRUTHY
        return expected if (raw == "on") == want_on else raw
    if f.field_type == "file":
        return expected if raw else ""
    return str(raw)


def _hash_by_key(values: list[PreviewValue]) -> str:
    """Step-independent comparison (a portal may renumber steps): non-empty values only."""
    return compute_preview_hash(
        PreviewValue(0, v.field_key, v.value) for v in values if normalize_value(v.value)
    )


def _hash_by_key_map(approved: dict[str, FieldResolution]) -> str:
    return compute_preview_hash(
        PreviewValue(0, k, r.value) for k, r in approved.items() if normalize_value(r.value)
    )


async def _fill(page: Any, f: FormField, res: FieldResolution, paths: dict[uuid.UUID, str]) -> None:
    value = res.value or ""
    if f.field_type == "file":
        if res.document_id is None or res.document_id not in paths:
            raise LookupError("document not available")
        await page.set_input_files(f.selector, paths[res.document_id])
    elif f.field_type == "select":
        try:
            await page.select_option(f.selector, label=value)
        except Exception:  # noqa: BLE001 - fall back to the option value
            await page.select_option(f.selector, value=value)
    elif f.field_type == "radio":
        clicked = await page.evaluate(
            """([name, wanted]) => {
                 for (const r of document.querySelectorAll(`input[type=radio][name="${CSS.escape(name)}"]`)) {
                   const label = (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)?.innerText.trim()) || r.closest('label')?.innerText.trim() || r.value;
                   if (label.toLowerCase() === wanted.toLowerCase() || r.value.toLowerCase() === wanted.toLowerCase()) { r.click(); return true; }
                 }
                 return false; }""",
            [f.key, value],
        )
        if not clicked:
            raise LookupError("radio option not found")
    elif f.field_type == "checkbox":
        if value.strip().lower() in TRUTHY:
            await page.check(f.selector)
    else:
        await page.fill(f.selector, value)


def _write_documents(
    documents: dict[uuid.UUID, DocumentBlob], folder: Path
) -> dict[uuid.UUID, str]:
    paths: dict[uuid.UUID, str] = {}
    for doc_id, blob in documents.items():
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", blob.filename)[:80] or "document.pdf"
        path = folder / f"{doc_id.hex[:8]}_{safe}"
        path.write_bytes(blob.data)
        paths[doc_id] = str(path)
    return paths
