"""Live campaign progress: the "current process" bar and the weighted end-to-end bar.

Pure function of the campaign phase and its counters, so the runner's `campaign.progress` SSE
events and the API's campaign views always agree. Crawl totals are a budget estimate (the crawl
may stop earlier); the crawl bar is capped below 100% until the crawl phase is over."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

# Counter keys maintained by the campaign runner for progress reporting.
PAGES_BUDGET = "pages_budget"
PAGES_FETCHED = "pages_fetched"
EXTRACT_QUEUED = "extract_queued"
EXTRACT_DONE = "extract_done"
STAGE_TARGET = "stage_target"
STAGE_DONE = "stage_done"
STAGE_READY = "stage_ready"

_OUTCOMES = ("eligible", "ineligible", "expired", "uncertain")
_CRAWLING = {"PENDING", "CRAWLING"}
_FINAL = {"COMPLETED", "AWAITING_OK"}
_STOPPED = {"FAILED", "CANCELLED"}
WEIGHTS_WITH_STAGING = (0.4, 0.3, 0.3)
WEIGHTS_WITHOUT_STAGING = (0.55, 0.45, 0.0)


@dataclass(frozen=True, slots=True)
class ProgressBar:
    label: str
    detail: str | None
    done: int
    total: int
    pct: float


@dataclass(frozen=True, slots=True)
class CampaignProgress:
    phase: str
    current: ProgressBar
    overall_pct: float
    crawled_pct: float
    evaluated_pct: float
    staged_pct: float
    crawled: int
    evaluated: int
    found: int
    staged: int
    stage_target: int
    auto_stage: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pct(done: int, total: int, *, cap: float = 100.0) -> float:
    if total <= 0:
        return 0.0
    return round(min(cap, 100.0 * done / total), 1)


def campaign_progress(
    phase: str,
    counters: Mapping[str, Any],
    *,
    auto_stage: bool,
    detail: str | None = None,
) -> CampaignProgress:
    def n(key: str) -> int:
        value = counters.get(key, 0)
        return int(value) if isinstance(value, int | float) else 0

    crawling = phase in _CRAWLING
    final = phase in _FINAL
    fetched, budget = n(PAGES_FETCHED), max(n(PAGES_BUDGET), n(PAGES_FETCHED))
    crawl_pct = _pct(fetched, budget, cap=99.0) if crawling else 100.0
    if phase == "PENDING":
        crawl_pct = 0.0

    queued, extracted = n(EXTRACT_QUEUED), n(EXTRACT_DONE)
    found = n("opportunities_found")
    evaluated = sum(n(k) for k in _OUTCOMES)
    if final:
        extract_pct = eval_pct = 100.0
    else:
        extract_cap = 99.0 if crawling else 100.0
        extract_pct = (
            _pct(extracted, queued, cap=extract_cap) if queued else (0.0 if crawling else 100.0)
        )
        eval_pct = _pct(evaluated, found, cap=extract_cap) if found else extract_pct
    rag_pct = round((extract_pct + eval_pct) / 2, 1)

    target, staged = n(STAGE_TARGET), n(STAGE_DONE)
    if final:
        stage_pct = 100.0
    elif phase == "FILLING":
        stage_pct = _pct(staged, target) if target else 100.0
    else:
        stage_pct = 0.0

    weights = WEIGHTS_WITH_STAGING if auto_stage else WEIGHTS_WITHOUT_STAGING
    overall = (
        100.0
        if final
        # 100% only once the campaign has completed (after its staging pass).
        else min(
            99.0,
            round(weights[0] * crawl_pct + weights[1] * rag_pct + weights[2] * stage_pct, 1),
        )
    )

    if crawling:
        current = ProgressBar(
            "Crawling, extracting and evaluating", detail, fetched, budget, crawl_pct
        )
    elif phase in ("EXTRACTING", "EVALUATING", "QUEUED"):
        current = ProgressBar(
            "Finishing extraction and eligibility", detail, extracted, queued, extract_pct
        )
    elif phase == "FILLING":
        current = ProgressBar("Auto-filling applications", detail, staged, target, stage_pct)
    elif final:
        label = "Awaiting your OK" if n(STAGE_READY) else "Completed"
        total = target if target else max(found, 1)
        current = ProgressBar(label, detail, total, total, 100.0)
    else:
        label = "Failed" if phase == "FAILED" else "Cancelled"
        current = ProgressBar(label, detail, fetched, budget, crawl_pct)

    return CampaignProgress(
        phase=phase,
        current=current,
        overall_pct=overall,
        crawled_pct=crawl_pct,
        evaluated_pct=rag_pct,
        staged_pct=stage_pct,
        crawled=fetched,
        evaluated=evaluated,
        found=found,
        staged=staged,
        stage_target=target,
        auto_stage=auto_stage,
    )
