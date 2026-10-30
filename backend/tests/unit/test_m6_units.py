"""Milestone 6 units: in-page RAG filter, RPM token bucket, PII redaction, evidence grounding,
document chunking, and the constitutional rule that no model id is hardcoded in source."""

from __future__ import annotations

import asyncio
import gzip
import re
import time
from pathlib import Path

import pytest

import tests.portal as portal
from app.domain.discovery.rag_filter import (
    SINGLE_WINDOW_CHARS,
    score_sections,
    select_sections,
    split_sections,
)
from app.domain.profile.evidence import (
    EVIDENCE_CONFIDENCE,
    chunk_document,
    combine_confidence,
    grounding_overlap,
    redact_pii,
)
from app.infrastructure.llm.rate_limiter import RateLimiterRegistry, SlidingWindowRateLimiter
from app.infrastructure.scraping.distiller import HtmlContentProcessor

ROOT = Path(__file__).resolve().parents[3]
HTML = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def _md(html: str, url: str) -> str:
    return HtmlContentProcessor().process(html, url).markdown


# ---- in-page RAG ---------------------------------------------------------------------------------


def test_long_detail_page_is_reduced_to_the_opportunity_sections() -> None:
    _, _, html = portal.render("/long-detail", "")
    markdown = _md(html, "http://portal/long-detail")
    selection = select_sections(score_sections(split_sections(markdown), ["research internship"]))
    assert selection.filtered and selection.single_window
    assert selection.chars_saved_pct > 80
    for needed in (
        "Title: Rho Global Research Fellowship",
        "Organization: Rho Institute",
        "Deadline: 2099-04-04",
    ):
        assert needed in selection.text
    assert "Comment by reader" not in selection.text
    assert "Related post" not in selection.text


def test_multi_opportunity_page_keeps_every_opportunity() -> None:
    html = gzip.decompress((HTML / "study_abroad_calendar.html.gz").read_bytes()).decode()
    markdown = _md(html, "https://www.study-abroad.org/blog/scholarship-deadlines-calendar-2026/")
    selection = select_sections(score_sections(split_sections(markdown), ["scholarship"]))
    for programme in ("MEXT", "Australia Awards", "Erasmus"):
        assert programme in selection.text
    assert selection.chars_after < selection.chars_before  # navigation still removed


def test_short_pages_are_never_filtered() -> None:
    sections = split_sections("# Title\n\nTitle: X\n\nDeadline: 2099-01-01\n")
    selection = select_sections(score_sections(sections, []))
    assert not selection.filtered and selection.chars_saved_pct == 0.0
    assert selection.chars_after <= SINGLE_WINDOW_CHARS


def test_reranker_scores_steer_the_selection() -> None:
    text = "\n\n".join(
        f"## Section {i}\n\n" + ("Neutral filler text about many unrelated topics. " * 30)
        for i in range(12)
    )
    sections = split_sections(text)
    rerank = [0.0] * len(sections)
    rerank[7] = 1.0  # the cross-encoder found the relevant passage
    selection = select_sections(score_sections(sections, [], rerank=rerank))
    assert selection.kept[0].section.heading == "Section 7" or any(
        k.section.heading == "Section 7" for k in selection.kept
    )
    top = max(selection.kept, key=lambda k: k.score)
    assert top.section.heading == "Section 7" and top.rerank == 1.0


# ---- RPM pacing ----------------------------------------------------------------------------------


async def test_token_bucket_paces_a_burst_without_exceeding_the_window() -> None:
    now = [0.0]
    starts: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    limiter = SlidingWindowRateLimiter(3, period=60.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(8):
        await limiter.acquire()
        starts.append(now[0])
    # Never more than 3 request starts in any rolling 60 s window.
    for i, t in enumerate(starts):
        assert sum(1 for u in starts if t <= u < t + 60.0) <= 3, (i, starts)
    assert starts[:3] == [0.0, 0.0, 0.0] and starts[3] == pytest.approx(60.0)
    assert starts[-1] == pytest.approx(120.0)  # 8 calls at 3/min: the third minute


async def test_concurrent_workers_share_one_limiter_fairly() -> None:
    limiter = SlidingWindowRateLimiter(5, period=0.4)
    started: list[float] = []

    async def call() -> None:
        await limiter.acquire()
        started.append(time.monotonic())

    t0 = time.monotonic()
    await asyncio.gather(*(call() for _ in range(12)))
    elapsed = time.monotonic() - t0
    assert 0.75 <= elapsed < 2.0  # 12 calls at 5 per 0.4 s: three windows, no stalls
    started.sort()
    for t in started:
        assert sum(1 for u in started if t <= u < t + 0.4 - 0.02) <= 5


def test_limiters_are_shared_per_host_and_key_and_follow_edits() -> None:
    registry = RateLimiterRegistry()
    a = registry.get("https://integrate.api.nvidia.com/v1", "nvapi-a", 36)
    b = registry.get("https://integrate.api.nvidia.com/v1/", "nvapi-a", 36)
    c = registry.get("https://integrate.api.nvidia.com/v1", "nvapi-other", 36)
    assert a is b and a is not c
    assert registry.get("https://integrate.api.nvidia.com/v1", "nvapi-a", 20).rpm == 20


# ---- evidence ------------------------------------------------------------------------------------


def test_pii_is_redacted_before_any_model_sees_a_snippet() -> None:
    snippet = (
        "Ayesha Khan | ayesha.khan@example.com | +92 300 5550199 | 35202-1234567-1\n"
        "Programming languages: Python, Rust | https://github.com/ayesha"
    )
    clean = redact_pii(snippet, ["Ayesha Khan"])
    for leaked in ("Ayesha", "example.com", "5550199", "35202", "github.com"):
        assert leaked not in clean
    assert "Programming languages: Python, Rust" in clean


def test_confidence_requires_grounding() -> None:
    evidence = ["Programming languages: Python, Rust, Go"]
    assert grounding_overlap("Python, Rust", evidence) == 1.0
    assert combine_confidence(0.92, grounding_overlap("Python, Rust", evidence)) >= (
        EVIDENCE_CONFIDENCE
    )
    hallucinated = grounding_overlap("Haskell and Erlang expert", evidence)
    assert combine_confidence(0.99, hallucinated) < EVIDENCE_CONFIDENCE


def test_documents_are_chunked_along_their_sections() -> None:
    text = "Education\nBSc Computer Science, GPA 3.6\n\nSkills\nPython, Rust\n\n" + (
        "Projects\n" + "Built a solar charger and a weather station. " * 40
    )
    chunks = chunk_document(text)
    assert len(chunks) >= 2 and all(len(c) <= 1_100 for c in chunks)
    assert any("GPA 3.6" in c for c in chunks)


# ---- constitution: user-provided models only ----------------------------------------------------

_MODEL_ID = re.compile(
    r"\b(nvidia|meta|mistralai|google|openai|qwen|deepseek|microsoft|baai|snowflake|z-ai|"
    r"moonshotai|ibm|nv-[a-z]+)/[a-z0-9][a-z0-9._-]+"
    r"|\bnemotron[a-z0-9.-]*|\bllama-?3[a-z0-9.-]*|\bgpt-[0-9][a-z0-9.-]*",
    re.I,
)


def test_no_model_id_is_hardcoded_in_source() -> None:
    offenders: list[str] = []
    for base, patterns in (
        (ROOT / "backend" / "src", ("*.py",)),
        (ROOT / "frontend" / "src", ("*.ts", "*.tsx")),
    ):
        for pattern in patterns:
            for path in base.rglob(pattern):
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if _MODEL_ID.search(line):
                        offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert offenders == [], "model ids must be user input, never source code:\n" + "\n".join(
        offenders
    )
