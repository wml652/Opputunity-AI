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

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "max_tokens": self.max_tokens,
            "level": self.level,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> ExtractionStrategy:
        if not data:
            return ExtractionStrategy()
        known = {k: data[k] for k in ExtractionStrategy().to_dict() if k in data}
        return ExtractionStrategy(**known)


TRANSIENT_FETCH_CODES = frozenset(
    {"http_403", "http_429", "http_503", "timeout", "empty_content", "network"}
)
PERMANENT_CODES = frozenset({"http_404", "http_410", "login_wall"})


def fetch_error_code(status: int | None, error: str | None, html_len: int) -> str | None:
    """Normalized failure code for a fetch, or None when it succeeded."""
    if error:
        low = error.lower()
        if "timeout" in low:
            return "timeout"
        if "ssrf" in low:
            return "ssrf_blocked"
        if "not html" in low:
            return "not_html"
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
        # Slow sites: stop waiting for full DOM parsing; render what arrived, then hydrate.
        return replace(
            current, wait_until="commit", timeout_ms=45_000, hydration_ms=4_000, level=nxt
        )
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
        )

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
