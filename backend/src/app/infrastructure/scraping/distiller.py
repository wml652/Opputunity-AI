"""HTML -> clean Markdown, after sanitization. trafilatura extracts the main content (dropping
navigation/boilerplate); a BeautifulSoup walker is the fallback for pages it cannot parse."""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from app.domain.discovery.listing import HarvestedCard, PageKind
from app.domain.ports.discovery import ProcessedPage
from app.infrastructure.scraping.sanitizer import clean_text, sanitize_html
from app.infrastructure.scraping.segmenter import ListingSegmenter

logging.getLogger("trafilatura").setLevel(logging.ERROR)

MAX_MARKDOWN_CHARS = 80_000
LISTING_INTRO_CHARS = 1_500
_BLANKS = re.compile(r"\n{3,}")


def _fallback_markdown(soup: BeautifulSoup, base_url: str) -> str:
    lines: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = " ".join(str(child).split())
                if text:
                    lines.append(text)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name or ""
            if re.fullmatch(r"h[1-6]", name):
                lines.append(
                    "\n" + "#" * int(name[1]) + " " + " ".join(child.get_text(" ").split()) + "\n"
                )
            elif name == "a" and child.get("href"):
                text = " ".join(child.get_text(" ").split())
                href = urljoin(base_url, str(child["href"]))
                lines.append(f"[{text}]({href})" if text else href)
            elif name == "li":
                lines.append("\n- " + " ".join(child.get_text(" ").split()))
            elif name in (
                "p",
                "div",
                "section",
                "article",
                "tr",
                "br",
                "table",
                "ul",
                "ol",
                "main",
            ):
                lines.append("\n")
                walk(child)
                lines.append("\n")
            else:
                walk(child)

    walk(soup.body or soup)
    text = " ".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return _BLANKS.sub("\n\n", text).strip()


class HtmlContentProcessor:
    """Sanitize -> classify -> distill. Article/detail pages go through trafilatura (BeautifulSoup
    walker as fallback). Hub/listing pages additionally run the DOM card segmenter, whose
    `[Title](URL) | Deadline | snippet` lines are appended so links and deadlines survive."""

    def __init__(self) -> None:
        self._segmenter = ListingSegmenter()

    def process(self, html: str, url: str) -> ProcessedPage:
        soup, flags = sanitize_html(html)
        title_tag = BeautifulSoup(html, "html.parser").find("title")
        title = clean_text(" ".join(title_tag.get_text(" ").split())) if title_tag else None
        prose = ""
        try:
            import trafilatura

            prose = (
                trafilatura.extract(
                    str(soup),
                    url=url,
                    output_format="markdown",
                    include_links=True,
                    include_tables=True,
                    include_comments=False,
                    favor_recall=True,
                )
                or ""
            )
        except Exception:  # noqa: BLE001 - fall back to the deterministic walker
            prose = ""
        used_fallback = False
        fallback = ""
        if len(prose) < 200:
            fallback = _fallback_markdown(soup, url)
            if len(fallback) > len(prose):
                prose, used_fallback = fallback, True
        else:
            # trafilatura sometimes picks a long "related posts" / comments block as the main
            # content and drops the real article. If the page's own <h1> title is missing from
            # its output but present in the full page, keep the full page: the in-page RAG filter
            # later removes the boilerplate before extraction.
            headline = _headline(soup)
            if headline and headline.lower() not in prose.lower():
                fallback = _fallback_markdown(soup, url)
                if headline.lower() in fallback.lower():
                    prose, used_fallback = fallback, True
        analysis = self._segmenter.analyze(soup, url)
        if analysis.kind == PageKind.LISTING:
            intro = prose[:LISTING_INTRO_CHARS].rsplit("\n", 1)[0] if prose else ""
            markdown = _compose(title, intro, analysis.cards)
        elif analysis.kind == PageKind.MIXED:
            markdown = _compose(None, prose, analysis.cards)
        else:
            markdown = prose
        markdown = clean_text(_BLANKS.sub("\n\n", markdown)).strip()[:MAX_MARKDOWN_CHARS]
        return ProcessedPage(
            title=title or None,
            markdown=markdown,
            content_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            injection_flags=flags,
            kind=analysis.kind,
            cards=analysis.cards,
            diagnostics={
                **analysis.stats,
                "prose_chars": len(prose),
                "prose_source": "bs_fallback" if used_fallback else "trafilatura",
            },
        )


def _headline(soup: BeautifulSoup) -> str | None:
    h1 = soup.find("h1")
    text = " ".join(h1.get_text(" ").split()) if h1 else ""
    return text if len(text) >= 8 else None


def _compose(title: str | None, prose: str, cards: list[HarvestedCard]) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if prose.strip():
        parts.append(prose.strip())
    if cards:
        parts.append(f"## Listing cards ({len(cards)})")
        parts.append("\n".join(card.to_markdown() for card in cards))
    return "\n\n".join(parts)


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) for visible links only (after sanitization)."""
    soup, _ = sanitize_html(html)
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        links.append(
            (urljoin(base_url, href).split("#", 1)[0], " ".join(a.get_text(" ").split())[:200])
        )
    return links
