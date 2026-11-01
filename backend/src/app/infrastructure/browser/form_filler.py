"""Playwright form filler: fills a multi-step portal up to - never through - the final Submit.

Per step: scan visible controls (with their human labels) -> ask the application's resolver ->
fill everything resolved with confidence >= 0.85 -> if a required field is still unknown, take a
checkpoint (encrypted storage_state + DOM fingerprint + fill ledger + screenshot) and PAUSE;
otherwise click Next/Continue. On reaching a step whose only way forward is Submit: checkpoint
and report READY_TO_SUBMIT.

Resume: restore storage_state and open the checkpoint URL. If the step's DOM fingerprint still
matches, continue there; if the portal lost the draft (fingerprint differs / redirected to the
start), replay from step 0 - the resolver returns the settled values for every earlier field.
Portal pathfinder: when the portal URL is an overview / directory page (no application form,
only lead-capture, newsletter, search or comment forms), the filler follows up to
MAX_ENTRY_HOPS goal-directed links (Overview -> How to apply -> Apply online -> form; new-tab
links, window.open popups, embedded ATS iframes, SPA hydration); the checkpoint then records
the form's URL. If no form is reached, the page is classified (PortalGate): sign-in /
registration wall, email-or-package-only, applications closed. Aggregator / blog hosts are
navigation-only: nothing is ever typed there (hard invariant, checked again in `_fill`). Lead / newsletter / search / comment forms are never
filled - the student's details only ever go into the application form itself.
All browser traffic goes through the SSRF egress proxy.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import time
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.domain.agent.models import NavAction, NavDecision, StepKind, TeachStep
from app.domain.applications.filling import (
    LOGIN_GATE_MESSAGE,
    CheckpointData,
    DocumentBlob,
    FieldResolution,
    FillOutcome,
    FillOutcomeKind,
    FormField,
    LiveStep,
    PortalGate,
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
from app.domain.discovery.listing import (
    MIN_PORTAL_LINK_SCORE,
    host_of,
    is_aggregator_host,
    score_outbound_link,
)
from app.domain.llm.provider import is_fatal
from app.domain.ports.agent import DecisionSink, DomRecall, Navigator
from app.domain.ports.applications import FillObserver, Resolver
from app.domain.training.models import Demonstration, DomState
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.fetchers import USER_AGENT
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

MAX_STEPS = 12
NAV_TIMEOUT_MS = 30_000
NEXT_RE = re.compile(
    r"\b(next|continue|proceed|save (and|&) (continue|next)|forward|next step)\b", re.I
)
SUBMIT_RE = re.compile(r"\b(submit|send application|apply now|finish)\b", re.I)
# A CAPTCHA blocks only when a challenge WIDGET is visible: most career sites load an invisible
# reCAPTCHA v3 / Turnstile script on every page, which must not stop a fill.
CAPTCHA_JS = r"""
() => {
  const seen = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width >= 60 && r.height >= 30 && s.visibility !== 'hidden' && s.display !== 'none'
      && s.opacity !== '0';
  };
  const frames = [...document.querySelectorAll('iframe[src]')].filter((f) =>
    /recaptcha\/(api2|enterprise)\/(anchor|bframe)|hcaptcha\.com\/captcha|challenges\.cloudflare\.com/i.test(f.src));
  if (frames.some(seen)) return true;
  const widgets = [...document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile')]
    .filter((w) => (w.getAttribute('data-size') || '') !== 'invisible');
  return widgets.some(seen);
}
"""


async def _captcha_visible(page: Any) -> bool:
    try:
        return bool(await page.evaluate(CAPTCHA_JS))
    except Exception:  # noqa: BLE001 - mid-navigation: not a captcha
        return False


CHALLENGE_RE = re.compile(
    r"(__cf_chl|cf-challenge|challenge-platform|cf_chl_opt|<title>\s*just a moment|"
    r"checking your browser|ddos-guard|perimeterx|px-captcha|incapsula)",
    re.I,
)
CHALLENGE_ERROR = (
    f"[{PortalGate.CHALLENGE.value}] the official site shows a bot check - click 'Sign In in "
    "Browser & Resume', pass it yourself, and filling continues with that session"
)
MAX_ENTRY_HOPS = 4
MAX_LLM_STEPS = 6  # agent decisions per run (DOM-RAG recalls + model calls)
# Tier 2: a taught demonstration this similar to the current page is acted on without the LLM.
RAG_ACT_SIMILARITY = 0.82
# In a form that is already being filled, these words mean "final submit": never clicked.
FINAL_WORDS = re.compile(
    r"\b(submit|send|lodge|finish|finalise|finalize|confirm|complete( my| the)? application|"
    r"apply now|apply)\b",
    re.I,
)
FRAME_INTERVAL_S = 0.6  # live viewport: at most this often (plus every hop / step / outcome)
FRAME_QUALITY = 65
MIN_PARTIAL_TEXT = 500  # a timed-out navigation is usable if the page shows this much text
ENTRY_LINK_MIN_SCORE = 3
HYDRATION_TIMEOUT_MS = 6_000  # SPA / ATS forms mount after DOMContentLoaded
POPUP_WAIT_MS = 1_200
NO_FORM_ERROR = (
    f"[{PortalGate.NO_FORM.value}] no application form found: this looks like an information "
    "page and no 'Apply now' / 'Official website' / 'How to apply' path led to a form - open it "
    "and find the official portal"
)
AGGREGATOR_ERROR = (
    f"[{PortalGate.AGGREGATOR.value}] this is an aggregator / blog page (never typed into) and "
    "it links to no official application page - find the programme's own website"
)
LOGIN_WALL_ERROR = LOGIN_GATE_MESSAGE


async def _is_login_wall(page: Any) -> bool:
    """A visible password field means a sign-in form: never fill it, never ask for it."""
    count = await page.locator("input[type=password]:visible").count()
    return bool(count)


TRUTHY = {"yes", "true", "1", "on", "y", "agree", "i agree"}

# Groups visible controls by their form (or the page), drops forms that are not applications
# (search, newsletter, lead capture / counselling, comments, contact) and marks the largest
# remaining group as the scope (data-ootg-scope) for scanning and buttons. Also picks up ARIA
# textbox / combobox / checkbox / switch widgets and contenteditable boxes.
SCOPE_JS = r"""
() => {
  const CONTROL = 'input, select, textarea, [contenteditable="true"], [contenteditable=""], ' +
    '[role=textbox], [role=combobox], [role=checkbox], [role=switch]';
  const SKIP_TYPES = ['hidden', 'submit', 'button', 'reset', 'image', 'search'];
  const NOT_APPLICATION = /\b(newsletter|subscri(be|ption)|alerts?|counsel+ing|consultation|consultant|expert advice|advice|get help|free (advice|session|assessment|guidance)|book (a )?(call|session|consultation)|call ?back|request (a )?(call|callback|info|information|brochure)|enquir\w*|inquir\w*|contact( us)?|get in touch|leave a (reply|comment)|post comment|comments?|search)\b/i;
  const SIDE = /(^|[\s_-])(side|sidebar|side-?form|widget|modal|popup|pop-up|overlay|footer|newsletter|subscri\w*|banner|cta|inquiry|enquiry|lead|chat)([\s_-]|$)/i;
  const APPLICATION = /\b(application|apply|applicant|candidate|enrol+ment|registration form|admission form)\b/i;
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && (el.offsetParent !== null || s.position === 'fixed');
  };
  const BUTTON = 'button, input[type=submit], input[type=button], [role=button]';
  // Loose controls (no <form>): group them by their nearest widget container - the closest
  // ancestor that also holds a heading or a button - instead of lumping the whole page together.
  const widget = (el) => {
    for (let n = el.parentElement, depth = 0; n && n !== document.body && depth < 8; n = n.parentElement, depth++) {
      if (n.querySelector('h1, h2, h3, h4, legend, [role=heading]') || n.querySelector(BUTTON)) return n;
    }
    return document.body;
  };
  document.querySelectorAll('[data-ootg-scope]').forEach((e) => e.removeAttribute('data-ootg-scope'));
  document.querySelectorAll('[data-ootg-excluded]').forEach((e) => e.removeAttribute('data-ootg-excluded'));
  const groups = new Map();
  for (const el of document.querySelectorAll(CONTROL)) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (SKIP_TYPES.includes(t)) continue;
      if (t !== 'file' && !visible(el)) continue;
    } else if (!visible(el)) continue;
    if (!['input', 'select', 'textarea'].includes(tag) && el.closest('input, select, textarea')) continue;
    const form = el.form || el.closest('form, [role=form]');
    const box = form || widget(el);
    if (!groups.has(box)) groups.set(box, { form: !!form, els: [] });
    groups.get(box).els.push(el);
  }
  const summaries = [];
  let best = null; let bestScore = -1;
  for (const [box, group] of groups) {
    const els = group.els;
    // Judge a form by its name, action, container and nearest heading - not by its field labels
    // (an application may well ask for "comments").
    let heading = '';
    for (let n = box; n && n !== document.documentElement && !heading; n = n.parentElement) {
      const h = n.querySelector('h1, h2, h3, h4, legend, [role=heading]');
      if (h) heading = h.innerText || '';
    }
    const buttons = [...box.querySelectorAll(BUTTON)].map((b) => [b.id, b.innerText || b.value || ''].join(' ')).join(' ');
    const head = [box.getAttribute('aria-label'), box.id, box.className && String(box.className),
                  box.getAttribute('action'), box.getAttribute('role'), heading, buttons,
                  (box.innerText || '').slice(0, 120)].filter(Boolean).join(' ');
    let sided = false;
    for (let n = box; n && n !== document.body; n = n.parentElement) {
      const tag = n.tagName.toLowerCase();
      if (['aside', 'footer', 'nav', 'header', 'dialog'].includes(tag) || n.getAttribute('role') === 'dialog' ||
          n.getAttribute('aria-modal') === 'true' || SIDE.test(`${n.id} ${n.className && String(n.className)}`)) { sided = true; break; }
    }
    const single = els.length <= 1;
    const searchy = box.getAttribute('role') === 'search' ||
      (single && els.every((e) => ['q', 's', 'search', 'query'].includes((e.name || '').toLowerCase())));
    const looksApp = APPLICATION.test(head);
    const excluded =
      searchy ||
      (NOT_APPLICATION.test(head) && !looksApp) ||
      (sided && !looksApp) ||
      // Loose controls outside any <form> need positive evidence of being an application.
      (!group.form && !looksApp) ||
      (box === document.body && single);
    summaries.push({ controls: els.length, excluded, form: group.form, label: head.slice(0, 80) });
    if (excluded) { if (box !== document.body) box.setAttribute('data-ootg-excluded', '1'); continue; }
    const score = els.length + (looksApp ? 5 : 0) + (group.form ? 1 : 0);
    if (score > bestScore) { best = box; bestScore = score; }
  }
  if (best) best.setAttribute('data-ootg-scope', '1');
  return { scoped: !!best, groups: summaries };
}
"""

SCAN_JS = r"""
() => {
  const scope = document.querySelector('[data-ootg-scope]');
  if (!scope) return [];
  const labelFor = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const parent = el.closest('label'); if (parent) return parent.innerText.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map((i) => document.getElementById(i)?.innerText.trim() || '').join(' ').trim(); if (t) return t; }
    return el.getAttribute('aria-label') || el.placeholder || el.getAttribute('data-placeholder') || el.name || el.title || '';
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && (el.offsetParent !== null || s.position === 'fixed');
  };
  const slug = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 60);
  const inScope = (el) => scope === document.body || scope.contains(el) || (el.form && el.form === scope);
  const out = []; const seen = new Set(); let anon = 0;
  const all = document.querySelectorAll('input, select, textarea, [contenteditable="true"], [contenteditable=""], [role=textbox], [role=combobox], [role=checkbox], [role=switch]');
  for (const el of all) {
    if (!inScope(el)) continue;
    const tag = el.tagName.toLowerCase();
    const native = ['input', 'select', 'textarea'].includes(tag);
    if (!native && el.closest('input, select, textarea')) continue;
    let type;
    if (native) {
      type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
      if (['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'].includes(type)) continue;
    } else {
      const role = el.getAttribute('role');
      type = role === 'checkbox' || role === 'switch' ? 'checkbox' : role === 'combobox' ? 'combobox' : 'textarea';
    }
    if (type !== 'file' && !visible(el)) continue;
    const label = labelFor(el);
    let key = el.name || el.id || (label ? 'aria_' + slug(label) : '');
    if (!key) key = 'control_' + (anon++);
    if (seen.has(key)) continue;
    seen.add(key);
    let selector;
    if (el.name) selector = `[name="${CSS.escape(el.name)}"]`;
    else if (el.id) selector = `[id="${CSS.escape(el.id)}"]`;
    else { el.setAttribute('data-ootg-key', key); selector = `[data-ootg-key="${CSS.escape(key)}"]`; }
    const required = !!el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio') {
      const group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(key)}"]`)];
      const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
      out.push({ key, selector, type: 'radio', label: legend ? legend.innerText.trim() : key,
                 options: group.map((r) => labelFor(r) || r.value), required: group.some((r) => r.required),
                 maxLength: null, accept: null });
      continue;
    }
    out.push({ key, selector, type: tag === 'select' ? 'select' : type, label,
               options: tag === 'select' ? [...el.options].filter((o) => o.value !== '').map((o) => o.text.trim()) : [],
               required, maxLength: el.maxLength > 0 ? el.maxLength : null, accept: el.accept || null });
  }
  return out;
}
"""

BUTTONS_JS = r"""
() => {
  const scope = document.querySelector('[data-ootg-scope]') || document.body || document.documentElement;
  // Buttons of excluded forms / widgets (lead capture, newsletter, search) and of sidebars,
  // footers and dialogs are never Next or Submit for the application.
  const foreign = (b) => b.closest('[data-ootg-excluded], aside, footer, nav, header, dialog, [role=dialog], [aria-modal=true]') && !b.closest('[data-ootg-scope]');
  const visible = (b) => b.offsetParent !== null && !b.disabled && b.getAttribute('aria-disabled') !== 'true' && !foreign(b);
  const pick = (root) => !root ? [] : [...root.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')].filter(visible);
  let found = pick(scope);
  if (!found.length && scope !== document.body) {
    // SPA layouts often keep Next / Submit outside the <form>: page buttons not inside other forms.
    found = pick(document.body || document.documentElement).filter((b) => !b.closest('form') || b.closest('form') === scope);
  }
  return found.map((b, i) => {
    b.setAttribute('data-ootg-btn', String(i));
    return { i, text: (b.innerText || b.value || b.getAttribute('aria-label') || '').trim() };
  });
}
"""

# Goal-directed navigation candidates on a page with no application form (links, buttons and
# embedded application iframes), scored in stages: entry points to the form itself first, then
# official-site links, then overview pages that lead towards it ("How to apply", "Admissions",
# "View open roles"). Python filters visited URLs and aggregator rules.
ENTRY_LINKS_JS = r"""
() => {
  const STAGES = [
    [/\b(apply now|apply online|apply here|apply today|apply manually|start (your |an |the |a new )?application|begin (your )?application|online application|application form|application portal|apply for this (job|position|role|program|programme|scholarship|opportunity)?|apply to this (job|position|role)|submit (an |your )?application)\b/i, 10],
    [/\b(official (website|site|link|portal|page)|visit (the )?official|go to (the )?official|program(me)? website|university website)\b/i, 8],
    [/^\s*apply\s*$/i, 7],
    [/\b(register now|sign up to apply|create (an )?account to apply)\b/i, 5],
    [/\b(how to apply|application process|application procedure|application instructions|apply to (the )?(program|programme|university|college)|admissions?|prospective students|international (students|applicants)|undergraduate admission|internships?|view (open |all )?(roles|positions|jobs|openings)|open (roles|positions)|job openings|current openings|search (jobs|openings)|students? (and|&) graduates|early careers?)\b/i, 4],
  ];
  const BAD = /\b(share|tweet|facebook|whatsapp|linkedin|telegram|pinterest|print|email this|subscribe|newsletter|comment|log ?in|sign ?in|related|previous|next post|privacy|cookie|terms)\b/i;
  const ATS = /(myworkdayjobs|greenhouse\.io|lever\.co|smartrecruiters|submittable|icims|taleo|successfactors|jobvite|ashbyhq|bamboohr|applytojob|technolutions|slate\.org|commonapp|typeform|jotform|formstack|forms\.gle|docs\.google\.com\/forms|forms\.office\.com)/i;
  const here = location.href.split('#')[0];
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll('a[href], button, [role=button], [role=link], input[type=button]')) {
    if (el.offsetParent === null) continue;
    if (el.closest('[data-ootg-excluded], nav[aria-label*=breadcrumb i], footer, ins.adsbygoogle, [id^=google_ads], [id^=div-gpt-ad], [class*=advert], [data-ad-slot]')) continue;
    if (/\b(sponsored|nofollow ugc)\b/i.test(el.getAttribute('rel') || '')) continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ');
    if (!text || text.length > 90 || BAD.test(text)) continue;
    const href = el.tagName.toLowerCase() === 'a' ? el.href : '';
    let score = 0;
    for (const [re, w] of STAGES) if (re.test(text)) { score = Math.max(score, w); }
    if (href && ATS.test(href)) score = Math.max(score, 6) + 3;
    if (!score) continue;
    let offsite = false;
    if (href) {
      if (/^(mailto|tel|javascript):/i.test(href)) continue;
      if (/[?&](gclid|gad_source|gbraid|adurl)=|\/aclk\?|doubleclick\.net|googleadservices/i.test(href)) continue;
      if (href.split('#')[0] === here) continue;
      try {
        const u = new URL(href);
        offsite = u.host !== location.host;
        if (/\/(apply|application|admission)/i.test(u.pathname)) score += 2;
      } catch (e) { continue; }
      if (offsite) score += 1;
    }
    el.setAttribute('data-ootg-entry', String(i));
    out.push({ i, text, href, score, offsite, kind: href ? 'link' : 'button' });
    i += 1;
  }
  for (const frame of document.querySelectorAll('iframe[src]')) {
    const src = frame.src || '';
    if (!/^https?:/i.test(src)) continue;
    if (ATS.test(src) || /(apply|application|form)/i.test(src)) {
      out.push({ i: -1, text: 'embedded application', href: src, score: 11, offsite: new URL(src).host !== location.host, kind: 'iframe' });
    }
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 12);
}
"""

# Why there is no form: sign-in / registration wall, email-only / document package, closed.
GATE_JS = r"""
() => {
  const text = (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 30000);
  const vis = (el) => el.offsetParent !== null;
  const hasPassword = [...document.querySelectorAll('input[type=password]')].some(vis);
  const LOGIN_TEXT = /\b(returning (users?|applicants?)|first[- ]time (users?|applicants?)|create (an |your )?account|new (users?|applicants?)|existing (users?|applicants?)|log ?in to (apply|continue|your|the)|sign ?in to (apply|continue|your|the)|register to apply|candidate (home|login|portal)|applicant (portal|login|account)|application portal login|sign in or create)\b/i;
  const LOGIN_LINK = /^\s*(log ?in|sign ?in|login here|log in here|sign in here|create (an |your )?account|register|returning users?:? log ?in)\s*$/i;
  const links = [...document.querySelectorAll('a[href], button, [role=button]')].filter(vis);
  const loginEls = links.filter((a) => LOGIN_LINK.test((a.innerText || a.getAttribute('aria-label') || '').trim()));
  const workday = document.querySelector('[data-automation-id=signInLink], [data-automation-id=createAccountLink], [data-automation-id=utilityButtonSignIn]');
  const portalish = /(apply|portal|application|admission|career|job|candidate)/i.test(location.href + ' ' + document.title);
  // Sign-in pages whose fields mount late (Taleo, SSO): the URL itself says so.
  const loginUrl = /(\/|=|\.)(log-?in|sign-?in|signin|accessmanagement|sso|auth\/|oauth|idp)/i.test(location.pathname + location.search);
  const login = hasPassword || !!workday || LOGIN_TEXT.test(text) || (loginEls.length > 0 && portalish) || loginUrl;
  const loginLink = loginEls.find((a) => a.tagName.toLowerCase() === 'a' && /^https?:/i.test(a.href) && !/create|register/i.test(a.innerText || ''))
    || loginEls.find((a) => a.tagName.toLowerCase() === 'a' && /^https?:/i.test(a.href));
  const CLOSED = /([^.!?]{0,120}\b(applications? (are|is) (now )?(closed|not (yet )?open)|(are|is) now closed|(are|is) closed for|no longer accepting( applications)?|(application|submission) (period|window|deadline) (has )?(closed|ended|passed)|applications? will (re)?open|applications? (open|opens) (on|in)|is (currently )?closed|currently not accepting|no (open|current) (positions|roles|vacancies|openings)|not accepting applications)\b[^.!?]{0,120}[.!?]?)/i;
  const closed = (text.match(CLOSED) || [null])[0];
  const EMAIL_CONTEXT = /\b(send|email|e-mail|mail|submit|forward)\b[^.]{0,160}\b(cv|resume|résumé|application|documents|statement|cover letter|transcripts?|portfolio)\b|\b(cv|resume|application|documents|statement)\b[^.]{0,160}\b(to|at)\b[^.]{0,40}@/i;
  const emails = [];
  if (EMAIL_CONTEXT.test(text)) {
    for (const m of text.matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)) if (!emails.includes(m[0])) emails.push(m[0]);
    for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
      const e = a.getAttribute('href').slice(7).split('?')[0];
      if (e && !emails.includes(e)) emails.push(e);
    }
  }
  const pkg = [...document.querySelectorAll('a[href]')].find((a) =>
    /\.(pdf|docx?)(\?|$)/i.test(a.href) && /\b(application form|apply|form)\b/i.test(a.innerText || ''));
  const PACKAGE = /\b(download (the )?application form|print (and|&) (complete|fill)|completed (application )?form (should|must) be (sent|submitted|emailed|mailed))\b/i;
  return {
    hasPassword, login, loginHref: loginLink ? loginLink.href : null,
    closed: closed ? closed.trim().slice(0, 240) : null,
    emails: emails.slice(0, 5),
    packageHref: pkg && PACKAGE.test(text) ? pkg.href : (pkg && emails.length ? pkg.href : null),
  };
}
"""


class PlaywrightFormFiller:
    def __init__(
        self,
        guard: SsrfGuard,
        *,
        headless: bool = True,
        aggregator_domains: frozenset[str] = frozenset(),
    ) -> None:
        self._guard = guard
        self._headless = headless
        self._aggregators = aggregator_domains

    async def run(
        self,
        portal_url: str,
        *,
        resume: ResumeState | None,
        resolver: Resolver,
        documents: dict[uuid.UUID, DocumentBlob],
        storage_state: str | None = None,
        observer: FillObserver | None = None,
        learned_path: Sequence[TeachStep] = (),
        navigator: Navigator | None = None,
        dom_recall: DomRecall | None = None,
        on_decision: DecisionSink | None = None,
    ) -> FillOutcome:
        token = _TELE.set(_Telemetry(observer))
        plan = _PLAN.set(_Plan(learned_path, navigator, dom_recall, on_decision))
        try:
            return await self._run(portal_url, resume, resolver, documents, storage_state)
        finally:
            _PLAN.reset(plan)
            _TELE.reset(token)

    async def _run(
        self,
        portal_url: str,
        resume: ResumeState | None,
        resolver: Resolver,
        documents: dict[uuid.UUID, DocumentBlob],
        storage_state: str | None,
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
            await _goto(page, resume.url)
            fields = await _scan(page)
            if fields and dom_fingerprint(fields) == resume.dom_fingerprint:
                step_index = resume.step_index
                log_lines.append(f"resumed at step {step_index + 1} (checkpoint intact)")
            else:
                replayed = True
                log_lines.append(
                    "portal lost the draft; replaying from step 1 using the fill ledger"
                )
                await _goto(page, portal_url)
        else:
            await _goto(page, portal_url)
        await _await_form(page)
        await _live(page, f"Opened {host_of(page.url)}", pct=6, force=True)
        if await _dismiss_cookies(page):
            log_lines.append("dismissed a cookie banner")
        plan = _PLAN.get()
        if resume is None and plan is not None and plan.path:
            page = await self._replay_path(page, context, plan.path, log_lines)

        completed = hops = 0
        visited = {_key(page.url), _key(portal_url)}
        for _ in range(MAX_STEPS + MAX_ENTRY_HOPS):
            html = await page.content()
            if CHALLENGE_RE.search(html) or CHALLENGE_RE.search(page.url):
                return await self._outcome(
                    FillOutcomeKind.BLOCKED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    fields=[],
                    error=CHALLENGE_ERROR,
                    gate=PortalGate.CHALLENGE.value,
                    gate_info={"sign_in_url": page.url},
                )
            if await _captcha_visible(page):
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
                    gate=PortalGate.LOGIN.value,
                    gate_info={"sign_in_url": page.url},
                )
            if completed == 0 and await _dismiss_cookies(page):
                log_lines.append("dismissed a cookie banner")
            # Hard invariant: on an aggregator / blog host nothing is scanned for filling and
            # nothing is typed - the only permitted action is following the official link.
            on_aggregator = is_aggregator_host(host_of(page.url), self._aggregators)
            fields = [] if on_aggregator else await _scan(page)
            buttons = [] if on_aggregator else await page.evaluate(BUTTONS_JS)
            has_nav = any(NEXT_RE.search(b["text"]) or SUBMIT_RE.search(b["text"]) for b in buttons)
            if (not fields or not has_nav) and completed == 0 and hops < MAX_ENTRY_HOPS:
                hopped = await _follow_entry_link(
                    page, context, self._guard, visited, self._aggregators
                )
                if hopped is not None:
                    page, note = hopped
                    hops += 1
                    page.set_default_timeout(NAV_TIMEOUT_MS)
                    log_lines.append(f"hop {hops}: {note}")
                    await _live(
                        page,
                        f"Hop {hops}/{MAX_ENTRY_HOPS} -> {host_of(page.url)}",
                        pct=_step_pct(hops, 0),
                        force=True,
                    )
                    continue
            if not fields and not has_nav and not on_aggregator and not ledger:
                verdict, page = await self._llm_step(
                    page,
                    context,
                    "reach this programme's application form and open its first step",
                    visited,
                    log_lines,
                    in_form=False,
                )
                if verdict == "moved":
                    continue
                if verdict == "login":
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
                        gate=PortalGate.LOGIN.value,
                        gate_info={"sign_in_url": page.url},
                    )
            if on_aggregator:
                return await self._outcome(
                    FillOutcomeKind.BLOCKED,
                    page,
                    context,
                    step_index,
                    ledger,
                    replayed,
                    completed,
                    log_lines,
                    fields=[],
                    error=AGGREGATOR_ERROR,
                    gate=PortalGate.AGGREGATOR.value,
                )
            if not fields and not has_nav:
                if ledger:
                    verdict, page = await self._llm_step(
                        page,
                        context,
                        "the form has been filled: move to the review step, or report that only "
                        "the final submit remains",
                        visited,
                        log_lines,
                        in_form=True,
                    )
                    if verdict == "final":
                        return await self._outcome(
                            FillOutcomeKind.READY_TO_SUBMIT,
                            page,
                            context,
                            step_index,
                            ledger,
                            replayed,
                            completed,
                            log_lines,
                        )
                    if verdict == "moved":
                        step_index += 1
                        completed += 1
                        continue
                return await self._gate_outcome(
                    page, context, step_index, ledger, replayed, completed, log_lines, visited
                )
            await _live(
                page,
                f"Scanning form - {len(fields)} field(s) on step {step_index + 1}",
                step_index=step_index,
                pct=_step_pct(hops, step_index),
                force=True,
            )
            resolutions = await resolver(step_index, fields)
            unresolved: list[UnresolvedField] = []
            title = await page.title()
            for n, (f, res) in enumerate(zip(fields, resolutions, strict=True), start=1):
                if res.fillable:
                    try:
                        await _fill(page, f, res, paths, self._aggregators)
                        await _live(
                            page,
                            f'Filling "{f.label[:60]}"',
                            step_index=step_index,
                            pct=_step_pct(hops, step_index, n, len(fields)),
                            field_label=f.label,
                            source=res.source.value,
                        )
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
                await _live(
                    page,
                    f"Step {step_index} complete -> next",
                    step_index=step_index,
                    pct=_step_pct(hops, step_index),
                    force=True,
                )
                continue
            if submit_btn is not None and not fields and not ledger:
                # A Submit button without any application field filled anywhere (e.g. a
                # directory page's inquiry widget) is not an application's final step.
                return await self._gate_outcome(
                    page, context, step_index, ledger, replayed, completed, log_lines, visited
                )
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
            verdict, page = await self._llm_step(
                page,
                context,
                "the visible fields are filled: go to the NEXT step of this application form "
                "(never the final submit)",
                visited,
                log_lines,
                in_form=True,
            )
            if verdict == "moved":
                step_index += 1
                completed += 1
                log_lines.append(f"completed step {step_index} (agent-chosen button)")
                continue
            if verdict == "final":
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
        observer: FillObserver | None = None,
        learned_path: Sequence[TeachStep] = (),
    ) -> SubmitOutcome:
        token = _TELE.set(_Telemetry(observer))
        plan = _PLAN.set(_Plan(learned_path, None))  # submit replays the path, never the LLM
        try:
            return await self._submit(portal_url, approved, approved_hash, storage_state, documents)
        finally:
            _PLAN.reset(plan)
            _TELE.reset(token)

    async def _submit(
        self,
        portal_url: str,
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
        await _goto(page, portal_url)
        await _await_form(page)
        await _dismiss_cookies(page)
        plan = _PLAN.get()
        if plan is not None and plan.path:
            page = await self._replay_path(page, page.context, plan.path, log_lines)
        visited = {_key(page.url), _key(portal_url)}
        for _ in range(MAX_ENTRY_HOPS):  # landing page -> the application form, as when filling
            if await _is_login_wall(page) or await _captcha_visible(page):
                break
            on_aggregator = is_aggregator_host(host_of(page.url), self._aggregators)
            fields = [] if on_aggregator else await _scan(page)
            buttons = [] if on_aggregator else await page.evaluate(BUTTONS_JS)
            if fields and any(
                NEXT_RE.search(b["text"]) or SUBMIT_RE.search(b["text"]) for b in buttons
            ):
                break
            hopped = await _follow_entry_link(
                page, page.context, self._guard, visited, self._aggregators
            )
            if hopped is None:
                break
            page, note = hopped
            log_lines.append(note)
        if is_aggregator_host(host_of(page.url), self._aggregators):
            return SubmitOutcome(SubmitOutcomeKind.BLOCKED, error=AGGREGATOR_ERROR, log=log_lines)
        observed: list[PreviewValue] = []
        seen: set[str] = set()
        for step_index in range(MAX_STEPS):
            if await _captcha_visible(page):
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
                    await _fill(page, f, res, paths, self._aggregators)
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
            await _live(
                page,
                f"Verifying step {step_index + 1} against your approved values",
                step_index=step_index,
                pct=_step_pct(0, step_index),
                force=True,
            )
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
            if submit_btn is None or (not fields and not seen):
                return SubmitOutcome(
                    SubmitOutcomeKind.BLOCKED,
                    error="no Next or Submit button on this step"
                    if submit_btn is None
                    else NO_FORM_ERROR,
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
        gate: str | None = None,
        gate_info: dict[str, Any] | None = None,
    ) -> FillOutcome:
        if fields is None:
            with contextlib.suppress(Exception):
                fields = await _scan(page)
        screenshot = None
        with contextlib.suppress(Exception):
            screenshot = await page.screenshot(full_page=True, type="png")
        label = {
            FillOutcomeKind.READY_TO_SUBMIT: "Ready for your OK - stopped before Submit",
            FillOutcomeKind.PAUSED: "Paused - a question needs your answer",
        }.get(kind, f"Blocked: {(error or 'needs a human')[:120]}")
        await _live(page, label, step_index=step_index, pct=100.0, final=True)
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
            gate=gate,
            gate_info=gate_info or {},
        )

    async def _replay_path(
        self, page: Any, context: Any, path: list[TeachStep], log_lines: list[str]
    ) -> Any:
        """Walk the route the student demonstrated in Teach Mode until a form appears."""
        for n, step in enumerate(path, start=1):
            if await _scan(page):
                break
            try:
                if step.kind in (StepKind.NAVIGATE, StepKind.OPEN_TAB) and step.url:
                    await self._guard.validate(step.url)
                    await _goto(page, step.url)
                elif step.kind is StepKind.CLICK:
                    moved = await _click_locator(page, context, step, self._guard)
                    if moved is None:
                        log_lines.append(f"learned step {n} not found: {step.locator.name[:60]}")
                        continue
                    page = moved
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - a stale step: the pathfinder continues
                log_lines.append(f"learned step {n} failed: {type(exc).__name__}")
                continue
            await _await_form(page)
            log_lines.append(
                f"replayed learned step {n}: {step.kind.value} {step.locator.name[:60]}"
            )
            await _live(page, f"Replaying learned step {n}/{len(path)}", pct=6 + n, force=True)
        return page

    async def _llm_step(
        self,
        page: Any,
        context: Any,
        goal: str,
        visited: set[str],
        log_lines: list[str],
        *,
        in_form: bool,
    ) -> tuple[str, Any]:
        """One LLM navigator decision: "moved" (clicked), "final" (only the final submit is
        left - never clicked), "login" (sign-in wall) or "none"."""
        plan = _PLAN.get()
        if plan is None or plan.llm_steps >= MAX_LLM_STEPS:
            return "none", page
        if plan.navigator is None and plan.recall is None:
            return "none", page
        plan.llm_steps += 1
        try:
            snap = await page.evaluate(NAV_SNAPSHOT_JS)
        except Exception:  # noqa: BLE001 - mid-navigation
            return "none", page
        signature = _dom_signature(snap)
        elements: list[dict[str, Any]] = list(snap.get("elements") or [])
        demos: list[Demonstration] = []
        if plan.recall is not None:
            try:
                demos = await plan.recall(signature)
            except Exception as exc:  # noqa: BLE001 - recall is help, never a crash
                log_lines.append(f"DOM-RAG unavailable: {type(exc).__name__}")
        decision: NavDecision | None = None
        tier = "llm"
        # Tier 2: a confident taught demonstration from ANY portal, matched by visible label.
        top = demos[0] if demos else None
        if (
            top is not None
            and top.similarity >= RAG_ACT_SIMILARITY
            and top.action.get("action") == "click"
        ):
            element = _match_element(elements, str(top.action.get("label") or ""))
            if element is not None:
                decision = NavDecision(
                    NavAction.CLICK, int(element["i"]), f"recalled from {top.domain}"
                )
                tier = "rag"
                await _live(page, f"Recalled taught step from {top.domain}", force=True)
        # Tier 3: the user's model, with the demonstrations as few-shot examples.
        if decision is None:
            if plan.navigator is None:
                return "none", page
            try:
                decision = await plan.navigator(
                    _format_snapshot(snap) + _demonstrations(demos), goal
                )
            except Exception as exc:  # noqa: BLE001 - the model is optional help, never a crash
                if is_fatal(exc):  # key / model rejected: stop asking it for this run
                    plan.navigator = None
                    log_lines.append(f"[MODEL] {getattr(exc, 'message', exc)}"[:300])
                    await _live(page, "Model rejected the request - check LLM Gateway", force=True)
                else:
                    log_lines.append(f"agent navigator unavailable: {type(exc).__name__}")
                return "none", page
            await _live(
                page, f"Agent decided: {decision.action.value.replace('_', ' ')}", force=True
            )
        log_lines.append(
            f"agent[{tier}]: {decision.action.value} {decision.index} - {decision.reason}"
        )
        if plan.on_decision is not None:
            chosen = next((e for e in elements if e.get("i") == decision.index), None)
            with contextlib.suppress(Exception):
                plan.on_decision(
                    signature,
                    {
                        "action": decision.action.value,
                        "label": str(chosen.get("text") or "") if chosen else "",
                        "role": str(chosen.get("role") or "") if chosen else "",
                        "tier": tier,
                        "url": page.url,
                    },
                )
        if decision.action is NavAction.NEEDS_HUMAN_LOGIN:
            return "login", page
        if decision.action is NavAction.REACHED_FINAL_REVIEW:
            return ("final" if in_form else "none"), page
        if decision.action not in (NavAction.CLICK, NavAction.DISMISS_COOKIE_BANNER):
            return "none", page
        element = next(
            (e for e in snap.get("elements") or [] if e.get("i") == decision.index), None
        )
        if element is None:
            return "none", page
        text = str(element.get("text") or "")
        if (
            in_form
            and decision.action is NavAction.CLICK
            and (FINAL_WORDS.search(text) and not NEXT_RE.search(text))
        ):
            # Safety: the human OK gate owns the final submit. Stop right before it.
            log_lines.append(f"stopped before '{text}' (final submit)")
            return "final", page
        if element.get("host") and is_aggregator_host(str(element["host"]), self._aggregators):
            return "none", page
        before, url_before = len(context.pages), page.url
        with contextlib.suppress(Exception):
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS // 3
            ):
                await page.click(f'[data-ootg-nav="{decision.index}"]', timeout=5_000)
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(POPUP_WAIT_MS)
        if len(context.pages) > before:
            page = context.pages[-1]
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded")
            await self._guard.validate(page.url)
        visited.add(_key(page.url))
        await _await_form(page)
        if decision.action is NavAction.DISMISS_COOKIE_BANNER:
            return "moved", page
        changed = _key(page.url) != _key(url_before) or len(context.pages) > before
        return ("moved" if changed or await _scan(page) else "none"), page

    async def _gate_outcome(
        self,
        page: Any,
        context: Any,
        step_index: int,
        ledger: list[dict[str, Any]],
        replayed: bool,
        completed: int,
        log_lines: list[str],
        visited: set[str],
    ) -> FillOutcome:
        gate, message, info = await _classify_gate(page, self._guard, visited, log_lines)
        log_lines.append(f"no fillable form: {gate}")
        return await self._outcome(
            FillOutcomeKind.BLOCKED,
            page,
            context,
            step_index,
            ledger,
            replayed,
            completed,
            log_lines,
            error=message,
            gate=gate,
            gate_info=info,
        )


async def _goto(page: Any, url: str) -> None:
    """Navigate; heavy pages (ads, trackers, megabyte HTML) may never reach DOMContentLoaded in
    time through the egress proxy - if the page already shows content, work with it."""
    try:
        await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        with contextlib.suppress(Exception):
            await page.wait_for_selector("body", timeout=10_000)
        text_len = 0
        with contextlib.suppress(Exception):
            text_len = int(await page.evaluate("() => (document.body?.innerText || '').length"))
        if text_len < MIN_PARTIAL_TEXT:
            raise
        log.info("goto_partial", url=url, text_chars=text_len)


class _Telemetry:
    """Per-run live-viewport state (one per fill / submit task, via a ContextVar, so parallel
    runs on the shared filler never mix)."""

    def __init__(self, observer: FillObserver | None) -> None:
        self.observer = observer
        self.last_frame = 0.0
        self.hops = 0
        self.pct = 2.0

    async def emit(
        self,
        page: Any,
        label: str,
        *,
        step_index: int = 0,
        pct: float | None = None,
        field_label: str | None = None,
        source: str | None = None,
        force: bool = False,
        final: bool = False,
    ) -> None:
        if self.observer is None:
            return
        if pct is not None:
            self.pct = max(self.pct, min(100.0, pct)) if not final else min(100.0, pct)
        frame = None
        now = time.monotonic()
        if force or final or now - self.last_frame >= FRAME_INTERVAL_S:
            self.last_frame = now
            with contextlib.suppress(Exception):
                frame = await page.screenshot(
                    type="jpeg", quality=FRAME_QUALITY, full_page=False, timeout=5_000
                )
        step = LiveStep(
            phase_label=label[:200],
            current_url=str(getattr(page, "url", ""))[:500],
            step_index=step_index,
            progress_pct=round(self.pct, 1),
            field_label=field_label[:200] if field_label else None,
            value_source=source,
            final=final,
        )
        with contextlib.suppress(Exception):  # telemetry never breaks a fill
            await self.observer(step, frame)


_TELE: ContextVar[_Telemetry | None] = ContextVar("ootg_fill_telemetry", default=None)


class _Plan:
    """Per-run agent plan: the path learned in Teach Mode and the LLM navigator."""

    def __init__(
        self,
        path: Sequence[TeachStep],
        navigator: Navigator | None,
        recall: DomRecall | None = None,
        on_decision: DecisionSink | None = None,
    ) -> None:
        self.path = list(path)
        self.navigator = navigator
        self.recall = recall
        self.on_decision = on_decision
        self.llm_steps = 0


_PLAN: ContextVar[_Plan | None] = ContextVar("ootg_fill_plan", default=None)

COOKIE_JS = r"""
() => {
  const words = /^(accept( all)?( cookies)?|allow( all)?( cookies)?|i agree|agree( and close)?|got it|ok,? got it|accept and close|i accept|yes,? i agree|accept & close|consent)$/i;
  const scopes = document.querySelectorAll('#onetrust-banner-sdk,#CybotCookiebotDialog,[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[aria-label*=cookie i],[role=dialog],[aria-modal=true]');
  for (const scope of scopes) {
    if (scope.closest('#__ootg_toolbar')) continue;
    const tag = (String(scope.id) + ' ' + String(scope.className)).toLowerCase();
    if (!/cookie|consent|privacy/.test((scope.innerText || '').toLowerCase()) && !/cookie|consent/.test(tag)) continue;
    for (const b of scope.querySelectorAll('button,[role=button],a,input[type=button],input[type=submit]')) {
      const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
      if (words.test(t) && b.offsetParent !== null) { b.click(); return t; }
    }
  }
  return null;
}
"""

NAV_SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-ootg-nav]').forEach((e) => e.removeAttribute('data-ootg-nav'));
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = []; let i = 0;
  for (const el of document.querySelectorAll('a[href],button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],input[type=button],summary')) {
    if (!vis(el) || el.closest('#__ootg_toolbar')) continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    if (!text) continue;
    el.setAttribute('data-ootg-nav', String(i));
    let host = ''; try { host = el.href ? new URL(el.href).host : ''; } catch (e) {}
    const banner = !!el.closest('[role=dialog],[aria-modal=true],[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i]');
    els.push({ i, text, role: el.getAttribute('role') || el.tagName.toLowerCase(), host, banner });
    i += 1; if (i >= 60) break;
  }
  const headings = [...document.querySelectorAll('h1,h2,h3')].filter(vis).map((h) => h.innerText.trim()).filter(Boolean).slice(0, 6);
  const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=password]),select,textarea')].filter(vis);
  const labelOf = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const parent = el.closest('label'); if (parent) return parent.innerText.trim();
    return el.getAttribute('aria-label') || el.placeholder || el.name || el.title || '';
  };
  const inputs = fields.length;
  const input_labels = fields.map(labelOf).filter(Boolean).slice(0, 40);
  return { title: document.title, url: location.href, headings, inputs, input_labels,
           text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 700), elements: els };
}
"""


def _dom_signature(snap: dict[str, Any]) -> str:
    return DomState.of(
        str(snap.get("url") or ""),
        str(snap.get("title") or ""),
        [str(h) for h in snap.get("headings") or []],
        [str(e.get("text") or "") for e in snap.get("elements") or []],
        [str(i) for i in snap.get("input_labels") or []],
    ).signature()


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _match_element(elements: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    """The current page's control with the demonstrated label (by visible text, never by CSS:
    another portal styles the same step differently)."""
    want = _norm(label)
    if not want:
        return None
    exact = [e for e in elements if _norm(e.get("text") or "") == want]
    if exact:
        return exact[0]
    near = [
        e
        for e in elements
        if len(want) >= 4
        and (want in _norm(e.get("text") or "") or _norm(e.get("text") or "") in want)
        and len(_norm(e.get("text") or "")) >= 4
    ]
    return near[0] if len(near) == 1 else None


def _demonstrations(demos: list[Demonstration]) -> str:
    if not demos:
        return ""
    lines = ["", "TAUGHT DEMONSTRATIONS (how the student handled the most similar pages before):"]
    for d in demos:
        heading = next(
            (ln for ln in d.dom_signature.splitlines() if ln.startswith("headings:")), ""
        )
        lines.append(
            f"- similarity {d.similarity:.2f} on {d.domain} ({heading[:120]}): "
            f"{d.action.get('action')} '{d.action.get('label', '')}'"
        )
    return "\n".join(lines)


def _format_snapshot(snap: dict[str, Any]) -> str:
    lines = [
        f"title: {snap.get('title', '')}",
        f"site: {host_of(str(snap.get('url', '')))}",
        f"headings: {' | '.join(snap.get('headings') or [])}",
        f"visible form inputs: {snap.get('inputs', 0)}",
        f"text: {snap.get('text', '')}",
        "elements:",
    ]
    for e in snap.get("elements") or []:
        extra = f" -> {e['host']}" if e.get("host") else ""
        flag = " [banner/dialog]" if e.get("banner") else ""
        lines.append(f"[{e['i']}] ({e['role']}) {e['text']}{extra}{flag}")
    return "\n".join(lines)


async def _dismiss_cookies(page: Any) -> str | None:
    try:
        clicked = await page.evaluate(COOKIE_JS)
    except Exception:  # noqa: BLE001 - mid-navigation
        return None
    if clicked:
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(500)
    return str(clicked) if clicked else None


async def _live(page: Any, label: str, **kwargs: Any) -> None:
    tele = _TELE.get()
    if tele is not None:
        await tele.emit(page, label, **kwargs)


def _step_pct(hops: int, step_index: int, done: int = 0, total: int = 0) -> float:
    within = (done / total) if total else 0.0
    return min(95.0, 8 + 5 * hops + 22 * step_index + 18 * within)


async def _await_form(page: Any) -> None:
    """Give SPA / ATS pages (Workday, Greenhouse, Lever, Slate...) a moment to mount the form."""
    with contextlib.suppress(Exception):
        await page.wait_for_function(
            """() => [...document.querySelectorAll('input:not([type=hidden]), select, textarea,
                     [contenteditable=true], [role=textbox]')].some((e) => e.offsetParent !== null)
                   || document.querySelector('[data-automation-id], #grnhse_app, .application-form,
                     form[action*="apply"], iframe[src*="greenhouse"], iframe[src*="lever"]')""",
            timeout=HYDRATION_TIMEOUT_MS,
        )


def _key(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


async def _follow_entry_link(
    page: Any,
    context: Any,
    guard: SsrfGuard,
    visited: set[str],
    aggregators: frozenset[str],
) -> tuple[Any, str] | None:
    """One goal-directed hop towards the application form. On an aggregator page only off-site
    official links may be followed. Returns the (possibly new) page and a description."""
    on_aggregator = is_aggregator_host(host_of(page.url), aggregators)
    candidates = await page.evaluate(ENTRY_LINKS_JS)
    for cand in candidates:
        if int(cand["score"]) < ENTRY_LINK_MIN_SCORE:
            break
        href = str(cand.get("href") or "")
        if on_aggregator and (
            not href
            or not cand.get("offsite")
            or score_outbound_link(href, str(cand["text"]), page.url, aggregators)
            < MIN_PORTAL_LINK_SCORE
        ):
            continue  # an aggregator is navigation-only: off-site to a scored official link
        try:
            if href:
                if _key(href) in visited:
                    continue
                await guard.validate(href)
                await _goto(page, href)  # also covers target="_blank" links: same tab
                visited.add(_key(page.url))
                await _await_form(page)
                return page, f"{cand['kind']} '{cand['text']}' -> {page.url}"
            before, url_before = len(context.pages), page.url
            with contextlib.suppress(Exception):
                await page.evaluate(
                    "(i) => document.querySelector(`[data-ootg-entry='${i}']`)"
                    "?.removeAttribute('target')",
                    cand["i"],
                )
            with contextlib.suppress(Exception):
                async with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS // 3
                ):
                    await page.click(f'[data-ootg-entry="{cand["i"]}"]')
            await page.wait_for_timeout(POPUP_WAIT_MS)
            if len(context.pages) > before:  # window.open(): continue in the new tab
                page = context.pages[-1]
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded")
                await guard.validate(page.url)
            elif _key(page.url) == _key(url_before) and not await _scan(page):
                continue  # the click changed nothing useful
            visited.add(_key(page.url))
            await _await_form(page)
            return page, f"clicked '{cand['text']}' -> {page.url}"
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            log.info("entry_link_failed", text=cand["text"], error=type(exc).__name__)
    return None


async def _classify_gate(
    page: Any, guard: SsrfGuard, visited: set[str], log_lines: list[str]
) -> tuple[str, str, dict[str, Any]]:
    """(PortalGate, user-facing reason, details) for an official page without a fillable form.
    A sign-in wall is followed one step so the checkpoint (and the 1-click browser capture)
    lands on the sign-in page itself."""
    g = await page.evaluate(GATE_JS)
    if g.get("closed"):
        quote = str(g["closed"])
        return (
            PortalGate.CLOSED.value,
            f'[{PortalGate.CLOSED.value}] the official page says: "{quote}"',
            {"quote": quote, "url": page.url},
        )
    if g.get("login"):
        href = g.get("loginHref")
        if not g.get("hasPassword") and href and _key(str(href)) not in visited:
            with contextlib.suppress(Exception):
                await guard.validate(str(href))
                await _goto(page, str(href))
                visited.add(_key(page.url))
                await _await_form(page)
                log_lines.append(f"opened the sign-in page {page.url}")
        return PortalGate.LOGIN.value, LOGIN_GATE_MESSAGE, {"sign_in_url": page.url}
    emails = [str(e) for e in g.get("emails") or []]
    if emails or g.get("packageHref"):
        return (
            PortalGate.EMAIL.value,
            f"[{PortalGate.EMAIL.value}] no online form: apply by email"
            + (f" to {', '.join(emails)}" if emails else " / document package"),
            {"recipients": emails, "form_url": g.get("packageHref"), "url": page.url},
        )
    return PortalGate.NO_FORM.value, NO_FORM_ERROR, {"url": page.url}


async def _click_locator(page: Any, context: Any, step: TeachStep, guard: SsrfGuard) -> Any | None:
    """Find a recorded element again (role + name, exact text, label, CSS, then its link) and
    click it; returns the page to continue on (a new tab if one opened) or None."""
    loc = step.locator
    candidates: list[Any] = []
    with contextlib.suppress(Exception):
        if loc.role and loc.name:
            candidates.append(page.get_by_role(loc.role, name=loc.name))
    if loc.name:
        candidates.append(page.get_by_text(loc.name, exact=True))
    if loc.label:
        candidates.append(page.get_by_label(loc.label))
    if loc.css:
        candidates.append(page.locator(loc.css))
    for candidate in candidates:
        try:
            target = candidate.first
            if not await target.count() or not await target.is_visible():
                continue
            before = len(context.pages)
            with contextlib.suppress(Exception):
                async with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS // 3
                ):
                    await target.click(timeout=5_000)
            await page.wait_for_timeout(POPUP_WAIT_MS)
            if len(context.pages) > before:
                new = context.pages[-1]
                with contextlib.suppress(Exception):
                    await new.wait_for_load_state("domcontentloaded")
                await guard.validate(new.url)
                return new
            return page
        except Exception:  # noqa: BLE001, S112 - try the next strategy
            continue
    if loc.href:
        await guard.validate(loc.href)
        await _goto(page, loc.href)
        return page
    return None


async def _scope(page: Any) -> bool:
    """Mark the application form on the page; False if there is none."""
    result = await page.evaluate(SCOPE_JS)
    return bool(result.get("scoped"))


async def _scan(page: Any) -> list[FormField]:
    await _scope(page)
    raw = await page.evaluate(SCAN_JS)
    return [
        FormField(
            key=str(r["key"]),
            label=str(r["label"] or r["key"])[:500],
            field_type=str(r["type"]),
            selector=str(r.get("selector") or f'[name="{r["key"]}"]'),
            options=[str(o) for o in r.get("options") or []],
            required=bool(r.get("required")),
            max_length=r.get("maxLength"),
            accept=r.get("accept"),
        )
        for r in raw
    ]


READ_JS = r"""
([selector, type]) => {
  const els = [...document.querySelectorAll(selector)];
  const el = els[0];
  if (!el) return null;
  if (type === 'select') { const o = el.options[el.selectedIndex]; return o ? JSON.stringify([o.text.trim(), o.value]) : ''; }
  if (type === 'radio') {
    const r = els.find((x) => x.checked); if (!r) return '';
    const label = (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)?.innerText.trim()) || r.closest('label')?.innerText.trim() || r.value;
    return JSON.stringify([label, r.value]);
  }
  if (type === 'checkbox') return (el.checked || el.getAttribute('aria-checked') === 'true') ? 'on' : '';
  if (!('value' in el)) return (el.innerText || '').trim();
  if (type === 'file') return el.files && el.files.length ? el.files[0].name : '';
  return el.value;
}
"""


async def _read_back(page: Any, f: FormField, expected: str) -> str:
    """What the page REALLY shows for this control, mapped onto the approved representation
    (select/radio: the option label or value; checkbox: truthiness; file: a file is attached)."""
    raw = await page.evaluate(READ_JS, [f.selector, f.field_type])
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


async def _fill(
    page: Any,
    f: FormField,
    res: FieldResolution,
    paths: dict[uuid.UUID, str],
    aggregators: frozenset[str] = frozenset(),
) -> None:
    if is_aggregator_host(host_of(page.url), aggregators):
        # Defense in depth for the hard invariant: student data never goes into an aggregator.
        raise PermissionError("refusing to type on an aggregator / directory host")
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
    elif f.field_type == "combobox":
        await page.click(f.selector)
        await page.fill(f.selector, value)
        await page.keyboard.press("Enter")
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
