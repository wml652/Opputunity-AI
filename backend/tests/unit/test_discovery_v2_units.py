"""Segmenter on real (frozen) pages from the forensic audit, fuzzy dedupe on the audit's real
duplicate pairs, hub link scoring, bot-challenge detection."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from app.domain.discovery.listing import (
    DedupeCandidate,
    LinkKind,
    PageKind,
    detail_link_score,
    find_deadline_snippet,
    is_near_duplicate,
    link_kind,
)
from app.domain.discovery.playbook import detect_bot_challenge
from app.infrastructure.scraping.distiller import HtmlContentProcessor

HTML = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def _process(name: str, url: str):  # type: ignore[no-untyped-def]
    html = gzip.decompress((HTML / f"{name}.html.gz").read_bytes()).decode("utf-8")
    return HtmlContentProcessor().process(html, url)


def test_opportunitiesradar_home_keeps_every_card_link() -> None:
    page = _process("opportunitiesradar_home", "https://opportunitiesradar.com/")
    # The audit measured 46 detail links in the HTML and 0 in trafilatura's Markdown.
    assert page.kind == PageKind.LISTING
    assert len(page.cards) >= 40
    assert all(c.url and c.url.startswith("https://opportunitiesradar.com/") for c in page.cards)
    assert sum(1 for c in page.cards if c.deadline_snippet) >= 40
    assert page.markdown.count("](https://opportunitiesradar.com/") >= 40
    titles = {c.title for c in page.cards}
    assert any(t.startswith("EPFL E3 Summer Internship 2027") for t in titles)
    assert not any("/page/" in (c.url or "") or "/tag/" in (c.url or "") for c in page.cards)


def test_scholarships_com_recovers_the_25_card_list() -> None:
    page = _process(
        "scholarships_com",
        "https://www.scholarships.com/financial-aid/college-scholarships/scholarships-by-grade",
    )
    assert page.kind == PageKind.LISTING
    by_title = {c.title: c for c in page.cards}
    hertz = by_title["Hertz Foundation Graduate Fellowship"]  # card 1 of the list
    assert hertz.deadline_snippet == "October 30, 2026"
    assert hertz.url and "/scholarships/hertz-foundation" in hertz.url
    assert sum(1 for c in page.cards if c.deadline_snippet) >= 25


def test_bold_org_listing_and_offsite_cards() -> None:
    page = _process("bold_org", "https://bold.org/scholarships/by-year/undergraduate-scholarships/")
    assert page.kind == PageKind.LISTING and len(page.cards) >= 40
    assert sum(1 for c in page.cards if c.deadline_snippet) >= 25


def test_calendar_article_keeps_its_prose() -> None:
    page = _process(
        "study_abroad_calendar",
        "https://www.study-abroad.org/blog/scholarship-deadlines-calendar-2026/",
    )
    assert page.kind in (PageKind.MIXED, PageKind.ARTICLE)  # never truncated to a card list
    assert len(page.markdown) >= 14_000
    assert "Australia Awards" in page.markdown and "MEXT" in page.markdown


def test_plain_article_is_not_a_listing() -> None:
    page = _process("iitk_rise", "https://www.iitk.ac.in/oir/iitk-rise")
    assert page.kind == PageKind.ARTICLE and page.cards == []
    assert len(page.markdown) > 1_000


def test_cloudflare_challenge_is_detected() -> None:
    html = gzip.decompress((HTML / "scholarshiptab_challenge.html.gz").read_bytes()).decode()
    assert detect_bot_challenge(403, html, {}) == "cloudflare"
    assert detect_bot_challenge(403, "<html>Forbidden</html>", {"cf-mitigated": "challenge"})
    assert detect_bot_challenge(200, "<html><title>MIT</title>Apply now</html>", {}) is None


@pytest.mark.parametrize(
    ("a", "b"),
    [  # real duplicate pairs from the teachwink.com extraction in the audit
        (("Daraz 11.11 Internship Program 2026", "Daraz"), ("11.11 Internship at Daraz", "Daraz")),
        (
            ("International Kangaroo Linguistic Contest 2026", "Kangaroo Pakistan"),
            ("International Kangaroo Linguistics Contest (IKLC) 2026", "Kangaroo Pakistan"),
        ),
        (
            (
                "Karachi Youth Summit 2026 - Volunteer & Ambassador Opportunities",
                "Alkhidmat Karachi",
            ),
            ("Karachi Youth Summit 2026 - Volunteers and Event Ambassadors", "Alkhidmat Karachi"),
        ),
        (
            ("YoungDev Paid Internships 2026", "YoungDev Intern"),
            ("YoungDev Paid Internships 2026", None),
        ),
    ],
)
def test_near_duplicates_are_merged(a: tuple[str, str | None], b: tuple[str, str | None]) -> None:
    assert is_near_duplicate(DedupeCandidate(a[0], a[1], None), DedupeCandidate(b[0], b[1], None))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("UNHCR Internships 2027", "UNHCR"), ("UNICEF Internships 2027", "UNICEF")),
        (("UNHCR Internships 2027", None), ("UNICEF Internships 2027", None)),
        (
            ("EPFL E3 Summer Internship 2027", "EPFL"),
            ("EPFL Summer Research Programme 2027", "EPFL"),
        ),
        (
            ("Zeta 1 Research Internship", "Zeta Institute"),
            ("Zeta 2 Research Internship", "Zeta Institute"),
        ),
        (("Omicron Grant 01", "Omicron Trust 01"), ("Omicron Grant 02", "Omicron Trust 02")),
    ],
)
def test_distinct_opportunities_are_kept(
    a: tuple[str, str | None], b: tuple[str, str | None]
) -> None:
    assert not is_near_duplicate(
        DedupeCandidate(a[0], a[1], None), DedupeCandidate(b[0], b[1], None)
    )


def test_hub_link_scoring_prefers_details_and_official_pages() -> None:
    hub = "https://opportunitiesradar.com/"
    kws = ["research internship"]
    detail = detail_link_score(
        "https://opportunitiesradar.com/epfl-e3-summer-internship-2027/", "EPFL E3", kws, hub
    )
    official = detail_link_score("https://www.ox.ac.uk/admissions/apply", "Apply online", kws, hub)
    assert detail > 0 and official > 0
    assert (
        detail_link_score("https://opportunitiesradar.com/category/x/page/38/", "38", kws, hub) < 0
    )
    assert detail_link_score("https://opportunitiesradar.com/tag/usa/", "USA", kws, hub) < detail
    assert detail_link_score("https://facebook.com/share", "Share", kws, hub) < 0
    assert link_kind("https://x.org/scholarships?page=2") == LinkKind.PAGINATION
    assert find_deadline_snippet("Amount $50,000 Deadline October 30, 2026 Since") == (
        "October 30, 2026"
    )
