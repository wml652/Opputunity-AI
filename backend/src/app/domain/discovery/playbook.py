"""Flight-recorder traces and cross-campaign self-healing (pure domain).

A `DomainPlaybook` remembers, per user and domain, which fetch/extraction strategy worked and
which URLs are permanently dead. `escalate_fetch` / `escalate_extraction` choose the next
strategy after a failure, so a page that failed one way is retried differently - within the same
run and in every later campaign."""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

# ---- trace --------------------------------------------------------------------------------------


class Stage(enum.StrEnum):
    DISCOVERY = "DISCOVERY"
    MEMORY = "MEMORY"
    PREFLIGHT_AND_SSRF = "PREFLIGHT_AND_SSRF"
    BROWSER_FETCH = "BROWSER_FETCH"
    HTTP_FETCH = "HTTP_FETCH"
    DISTILL_AND_SANITIZE = "DISTILL_AND_SANITIZE"
    HUB_HARVEST = "HUB_HARVEST"  # listing page: cards harvested, detail links queued
    PDF_PARSE = "PDF_PARSE"  # an opportunity published as a PDF brochure / call document
    VISION_PARSE = "VISION_PARSE"  # image/table-only page read by the user's vision model
    RAG_FILTER = "RAG_FILTER"  # only high-signal sections of a long page reach the LLM
    LLM_EXTRACTION = "LLM_EXTRACTION"


class StepStatus(enum.StrEnum):
    OK = "ok"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(slots=True)
class TraceStep:
    stage: Stage
    status: StepStatus = StepStatus.OK
    started_at: str = ""
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


class Trace:
    def __init__(self, steps: list[dict[str, Any]] | None = None) -> None:
        self.steps: list[dict[str, Any]] = list(steps or [])

    @contextmanager
    def step(self, stage: Stage, **detail: Any) -> Iterator[TraceStep]:
        record = TraceStep(
            stage=stage, started_at=datetime.now(UTC).isoformat(), detail=dict(detail)
        )
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = StepStatus.FAIL
            record.detail.setdefault("error", f"{type(exc).__name__}: {str(exc)[:300]}")
            raise
        finally:
            record.duration_ms = round((time.perf_counter() - started) * 1000)
            self.steps.append(record.to_dict())

    def add(self, stage: Stage, status: StepStatus, **detail: Any) -> None:
        self.steps.append(
            TraceStep(stage, status, datetime.now(UTC).isoformat(), 0, dict(detail)).to_dict()
        )

    def extend(self, steps: list[dict[str, Any]]) -> None:
        self.steps.extend(steps)


# ---- strategies ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchStrategy:
    mode: str = "browser"  # browser | http
    wait_until: str = "domcontentloaded"  # commit | domcontentloaded | load | networkidle
    timeout_ms: int = 25_000
    hydration_ms: int = 1_500
    backoff_s: float = 0.0  # wait before fetching (rate-limited domains)
    level: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "wait_until": self.wait_until,
            "timeout_ms": self.timeout_ms,
            "hydration_ms": self.hydration_ms,
            "backoff_s": self.backoff_s,
            "level": self.level,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> FetchStrategy:
        if not data:
            return FetchStrategy()
        known = {k: data[k] for k in FetchStrategy().to_dict() if k in data}
        return FetchStrategy(**known)


@dataclass(frozen=True, slots=True)
class ExtractionStrategy:
    chunk_chars: int = 6_000  # pages longer than this are extracted in windows
    overlap_chars: int = 400
    max_tokens: int = 8_192  # room for reasoning models that think before answering
    level: int = 0
    # Per-page window budget. Pages beyond it are NOT silently cut: the trace records
    # `truncated_windows` and the inspector flags the page.
    max_windows: int = 24

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "max_tokens": self.max_tokens,
            "level": self.level,
            "max_windows": self.max_windows,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> ExtractionStrategy:
        if not data:
            return ExtractionStrategy()
        known = {k: data[k] for k in ExtractionStrategy().to_dict() if k in data}
        return ExtractionStrategy(**known)


TRANSIENT_FETCH_CODES = frozenset(
    {
        "http_403",
        "http_429",
        "http_503",
        "timeout",
        "empty_content",
        "network",
        "dns_error",
        "tunnel_connect_error",
    }
)
PERMANENT_CODES = frozenset({"http_404", "http_410", "login_wall"})
# Domain-level: a bot-protection challenge (Cloudflare "Just a moment...") cannot be passed by
# automation (and must not be: no CAPTCHA/challenge solving). Remembered per domain so later
# campaigns skip it at once, unless the user solved it in a headful browser and saved a session.
DOMAIN_BLOCKING_CODES = frozenset({"bot_challenge"})

_CHALLENGE_MARKERS = (
    "<title>just a moment...</title>",
    "<title>just a moment…</title>",
    "checking your browser",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "challenge-platform",
    "_cf_chl_opt",
    "cf_chl_",
    "ddos-guard",
    "please enable cookies and javascript to continue",
    "verify you are human",
    "px-captcha",
)


def detect_bot_challenge(status: int | None, html: str, headers: dict[str, str]) -> str | None:
    """Returns the protection vendor when the response is an interstitial bot challenge."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if lowered.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare"
    head = html[:20_000].lower()
    if any(marker in head for marker in _CHALLENGE_MARKERS):
        if "ddos-guard" in head:
            return "ddos-guard"
        if "px-captcha" in head:
            return "perimeterx"
        return "cloudflare"
    if (
        status in (403, 503)
        and "cloudflare" in lowered.get("server", "").lower()
        and len(html) < 40_000
    ):
        return "cloudflare"
    return None


def fetch_error_code(status: int | None, error: str | None, html_len: int) -> str | None:
    """Normalized failure code for a fetch, or None when it succeeded. Network-level codes
    (`dns_error`, `tunnel_connect_error`) are kept apart from real `ssrf_blocked` violations."""
    if error:
        low = error.lower()
        for code in ("bot_challenge", "dns_error", "tunnel_connect_error", "login_wall"):
            if low.startswith(code):
                return code
        if "timeout" in low:
            return "timeout"
        if "ssrf" in low:
            return "ssrf_blocked"
        if "not html" in low:
            return "not_html"
        if "err_name_not_resolved" in low:
            return "dns_error"
        if "err_tunnel_connection_failed" in low or "err_proxy_connection_failed" in low:
            return "tunnel_connect_error"
        return "network"
    if status is not None and status >= 400:
        return f"http_{status}"
    if html_len < 200:
        return "empty_content"
    return None


def escalate_fetch(current: FetchStrategy, code: str) -> FetchStrategy | None:
    """Next strategy to try after `code`, or None when retrying cannot help."""
    if code not in TRANSIENT_FETCH_CODES or current.level >= 3:
        return None
    nxt = current.level + 1
    if code == "http_429":
        return replace(current, backoff_s=max(5.0, current.backoff_s * 2 or 5.0), level=nxt)
    if code == "timeout":
        # Slow sites: the browser already fell back to plain HTTP inside the same attempt
        # (fast path for static pages). Next: let the full `load` event fire with more time;
        # never snapshot at `commit`, which captured empty shells.
        return replace(
            current,
            mode="browser",
            wait_until="load",
            timeout_ms=45_000,
            hydration_ms=3_000,
            level=nxt,
        )
    if code in ("dns_error", "tunnel_connect_error"):
        return replace(current, backoff_s=max(2.0, current.backoff_s * 2 or 2.0), level=nxt)
    if code == "empty_content":
        return replace(
            current,
            mode="browser",
            wait_until="networkidle",
            hydration_ms=5_000,
            timeout_ms=40_000,
            level=nxt,
        )
    if code == "http_403":
        # Alternate between the real browser and a plain request with browser headers.
        return replace(
            current,
            mode="http" if current.mode == "browser" else "browser",
            hydration_ms=3_000,
            level=nxt,
        )
    return replace(current, timeout_ms=40_000, level=nxt)


def escalate_extraction(current: ExtractionStrategy) -> ExtractionStrategy | None:
    if current.level >= 2:
        return None
    return replace(
        current,
        chunk_chars=max(2_500, current.chunk_chars // 2),
        max_tokens=min(16_384, current.max_tokens * 2),
        level=current.level + 1,
    )


# ---- playbook -----------------------------------------------------------------------------------


@dataclass(slots=True)
class DomainPlaybook:
    id: uuid.UUID
    user_id: uuid.UUID
    domain: str
    fetch: FetchStrategy
    extraction: ExtractionStrategy
    failures: dict[str, int]  # "STAGE:code" -> count
    dead_urls: dict[str, dict[str, Any]]  # url -> {code, campaign_id, at}
    successes: int
    last_campaign_id: uuid.UUID | None
    updated_at: datetime
    # {"vendor", "campaign_id", "at", "url"} once a bot challenge was seen; cleared when a
    # user-solved browser session exists for the domain.
    bot_challenge: dict[str, Any] | None = None

    @staticmethod
    def new(user_id: uuid.UUID, domain: str, now: datetime) -> DomainPlaybook:
        return DomainPlaybook(
            id=uuid.uuid4(),
            user_id=user_id,
            domain=domain,
            fetch=FetchStrategy(),
            extraction=ExtractionStrategy(),
            failures={},
            dead_urls={},
            successes=0,
            last_campaign_id=None,
            updated_at=now,
            bot_challenge=None,
        )

    def mark_challenged(self, vendor: str, url: str, campaign_id: uuid.UUID, at: datetime) -> None:
        self.bot_challenge = {
            "vendor": vendor,
            "url": url,
            "campaign_id": str(campaign_id),
            "at": at.isoformat(),
        }

    def record_failure(self, stage: Stage, code: str, campaign_id: uuid.UUID) -> None:
        key = f"{stage.value}:{code}"
        self.failures[key] = self.failures.get(key, 0) + 1
        self.last_campaign_id = campaign_id

    def mark_dead(self, url: str, code: str, campaign_id: uuid.UUID, at: datetime) -> None:
        self.dead_urls[url] = {"code": code, "campaign_id": str(campaign_id), "at": at.isoformat()}
        if len(self.dead_urls) > 500:
            oldest = sorted(self.dead_urls.items(), key=lambda kv: kv[1]["at"])[
                : len(self.dead_urls) - 500
            ]
            for url_key, _ in oldest:
                self.dead_urls.pop(url_key, None)

    def dead_reason(self, url: str) -> dict[str, Any] | None:
        return self.dead_urls.get(url)


def normalize_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")
