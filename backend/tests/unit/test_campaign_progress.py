from __future__ import annotations

from app.domain.discovery.progress import campaign_progress


def test_crawl_phase_tracks_pages_against_the_budget_and_never_claims_done() -> None:
    p = campaign_progress(
        "CRAWLING",
        {"pages_budget": 100, "pages_fetched": 40, "extract_queued": 30, "extract_done": 10},
        auto_stage=True,
        detail="crawled https://example.org/a",
    )
    assert p.current.label == "Crawling, extracting and evaluating"
    assert (p.current.done, p.current.total, p.current.pct) == (40, 100, 40.0)
    assert p.current.detail == "crawled https://example.org/a"
    assert p.crawled_pct == 40.0 and p.overall_pct < 40.0
    over = campaign_progress("CRAWLING", {"pages_budget": 10, "pages_fetched": 30}, auto_stage=True)
    assert over.current.total == 30 and over.crawled_pct == 99.0  # budget grows, never 100


def test_filling_phase_and_weights() -> None:
    counters = {
        "pages_budget": 50,
        "pages_fetched": 20,
        "extract_queued": 12,
        "extract_done": 12,
        "opportunities_found": 4,
        "eligible": 2,
        "ineligible": 2,
        "stage_target": 4,
        "stage_done": 1,
    }
    p = campaign_progress("FILLING", counters, auto_stage=True, detail="filling Zeta")
    assert p.current.label == "Auto-filling applications" and p.current.pct == 25.0
    assert p.crawled_pct == 100.0 and p.evaluated_pct == 100.0 and p.staged_pct == 25.0
    assert p.overall_pct == round(0.4 * 100 + 0.3 * 100 + 0.3 * 25, 1)
    done = campaign_progress("FILLING", {**counters, "stage_done": 4}, auto_stage=True)
    assert done.current.pct == 100.0 and done.overall_pct == 99.0  # 100 only when completed
    finished = campaign_progress("COMPLETED", {**counters, "stage_ready": 3}, auto_stage=True)
    assert finished.overall_pct == 100.0 and finished.current.label == "Awaiting your OK"


def test_without_auto_stage_discovery_and_extraction_carry_all_the_weight() -> None:
    p = campaign_progress(
        "EXTRACTING",
        {"pages_fetched": 10, "extract_queued": 10, "extract_done": 5},
        auto_stage=False,
    )
    assert p.current.label == "Finishing extraction and eligibility" and p.current.pct == 50.0
    assert p.overall_pct == round(0.55 * 100 + 0.45 * 50, 1)
    assert campaign_progress("PENDING", {}, auto_stage=True).overall_pct == 0.0
