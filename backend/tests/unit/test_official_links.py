"""Aggregator detection, outbound official-link scoring and portal resolution (pure domain),
plus the distiller section the extractor reads and the RAG filter keeps."""

from __future__ import annotations

from app.domain.discovery.listing import (
    OUTBOUND_HEADING,
    best_outbound_links,
    is_aggregator_url,
    outbound_markdown,
    parse_outbound_links,
    resolve_portal_url,
    score_outbound_link,
)
from app.domain.discovery.rag_filter import score_sections, select_sections, split_sections
from app.infrastructure.scraping.distiller import HtmlContentProcessor

PAGE = "https://www.studyabroad.pk/scholarships/duke-karsh-international-scholarship-usa-2027"


def test_aggregators_are_recognised_including_subdomains_and_extras() -> None:
    assert is_aggregator_url(PAGE)
    assert is_aggregator_url("https://blog.opportunitiesradar.com/x")
    assert not is_aggregator_url("https://ousf.duke.edu/merit-scholarships/")
    assert is_aggregator_url("http://127.0.0.2:8080/a", frozenset({"127.0.0.2"}))


def test_official_and_ats_links_outrank_everything_else() -> None:
    anchors = [
        ("https://ousf.duke.edu/merit-scholarships/karsh/", "Apply Now"),
        ("https://www.facebook.com/sharer/sharer.php?u=x", "Share"),
        ("https://www.studyabroad.pk/scholarships", "Apply"),  # same aggregator site
        ("https://scholarshiptab.com/x", "Official Link"),  # another aggregator
        ("https://boards.greenhouse.io/acme/jobs/1", "Apply for this job"),
        ("https://www.duke.edu/news", "News"),
    ]
    links = best_outbound_links(anchors, PAGE)
    assert [link.url for link in links] == [
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://ousf.duke.edu/merit-scholarships/karsh/",
        "https://www.duke.edu/news",
    ]
    assert score_outbound_link("https://admissions.berea.edu/portal/x", "Official Link", PAGE) > 15
    assert score_outbound_link("mailto:a@b.edu", "Apply", PAGE) == 0


def test_markdown_section_round_trips_and_resolution_rules() -> None:
    links = best_outbound_links([("https://ousf.duke.edu/karsh/", "Official Link")], PAGE)
    md = "# Duke Karsh\n\nSome text\n\n" + outbound_markdown(links)
    assert parse_outbound_links(md) == links
    pick = resolve_portal_url(PAGE, PAGE, links, title="Duke Karsh", organization=None, siblings=1)
    assert pick == "https://ousf.duke.edu/karsh/"
    assert resolve_portal_url(None, PAGE, links, title="X", organization=None, siblings=1) == pick
    official = "https://apply.example.edu/form"
    assert (
        resolve_portal_url(official, PAGE, links, title="X", organization=None, siblings=1) is None
    )  # an official portal is kept
    # On a page listing several opportunities, the link must name this one.
    assert (
        resolve_portal_url(
            PAGE, PAGE, links, title="Berea College Grant", organization=None, siblings=3
        )
        is None
    )
    assert (
        resolve_portal_url(
            PAGE, PAGE, links, title="Duke Karsh Scholarship", organization="Duke", siblings=3
        )
        == pick
    )


def test_distiller_appends_the_section_and_rag_keeps_it() -> None:
    filler = "<p>" + ("Unrelated words about campus life and many other topics. " * 40) + "</p>"
    html = (
        "<html><head><title>Duke Karsh International Scholarship</title></head><body>"
        "<article><h1>Duke Karsh International Scholarship USA 2027</h1>"
        "<p>Deadline: 2099-01-01. Degree: Bachelor.</p>"
        f"{''.join(f'<h2>Section {i}</h2>{filler}' for i in range(6))}"
        "<p><a href='https://ousf.duke.edu/merit-scholarships/karsh/'>Apply Now</a> "
        "<a href='https://www.facebook.com/sharer'>Share</a></p></article></body></html>"
    )
    processed = HtmlContentProcessor().process(html, PAGE)
    assert processed.outbound_links[0].url == "https://ousf.duke.edu/merit-scholarships/karsh/"
    assert processed.markdown.rstrip().endswith("karsh/) (score 15)")
    assert OUTBOUND_HEADING in processed.markdown
    selection = select_sections(score_sections(split_sections(processed.markdown), ["phd"]))
    assert selection.filtered and OUTBOUND_HEADING in selection.text


def test_ad_clicks_are_never_official_links_but_official_job_sites_are() -> None:
    ad = "https://en.jiagle.com/lighting-ezbuy/?gad_source=5&gclid=EAIaIQob"
    assert score_outbound_link(ad, "Apply Now", PAGE) == 0
    assert (
        score_outbound_link("https://www.googleadservices.com/pagead/aclk?x=1", "Apply", PAGE) == 0
    )
    assert (
        score_outbound_link("https://www.amazon.jobs/en/teams/internships", "Apply Now", PAGE) > 6
    )
    assert score_outbound_link("https://www.amazon.com/dp/B000", "Apply", PAGE) == 0
