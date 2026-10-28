"""HTML -> clean Markdown, after sanitization. trafilatura extracts the main content (dropping
navigation/boilerplate); a BeautifulSoup walker is the fallback for pages it cannot parse."""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from app.domain.ports.discovery import ProcessedPage
from app.infrastructure.scraping.sanitizer import clean_text, sanitize_html

logging.getLogger("trafilatura").setLevel(logging.ERROR)

MAX_MARKDOWN_CHARS = 60_000
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
    def process(self, html: str, url: str) -> ProcessedPage:
        soup, flags = sanitize_html(html)
        title_tag = BeautifulSoup(html, "html.parser").find("title")
        title = clean_text(" ".join(title_tag.get_text(" ").split())) if title_tag else None
        markdown = ""
        try:
            import trafilatura

            markdown = (
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
            markdown = ""
        if len(markdown) < 200:
            fallback = _fallback_markdown(soup, url)
            if len(fallback) > len(markdown):
                markdown = fallback
        markdown = clean_text(_BLANKS.sub("\n\n", markdown)).strip()[:MAX_MARKDOWN_CHARS]
        return ProcessedPage(
            title=title or None,
            markdown=markdown,
            content_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            injection_flags=flags,
        )


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
