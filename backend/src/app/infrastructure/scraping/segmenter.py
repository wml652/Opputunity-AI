"""DOM card & table segmenter for hub / listing pages.

trafilatura is built for article bodies and drops link-dense card grids as boilerplate (the
audit measured 46 -> 0 links on opportunitiesradar.com, 70 -> 11 on scholarships.com). This
segmenter works on the sanitized DOM instead: it finds anchors that look like opportunity
detail links, grows each one into its card container (the largest ancestor that still holds only
that one card), and keeps `title + absolute URL + snippet + deadline` together.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from app.domain.discovery.listing import (
    HarvestedCard,
    LinkKind,
    PageKind,
    find_deadline_snippet,
    link_kind,
)

MIN_CARDS_FOR_LISTING = 6
MAX_CARD_TEXT = 900
MAX_CARDS = 400
_CHROME = ("nav", "footer", "form", "dialog")
_CHROME_ROLES = re.compile(r"navigation|banner|contentinfo|search|menu", re.I)
# Matched against WHOLE class tokens: "td_with_ajax_pagination" is a content block that merely
# supports paging, while "pagination" / "site-footer" / "main-nav" are chrome.
_CHROME_CLASS = re.compile(
    r"^(?:site|main|primary|top|global|header|footer)?[-_]?"
    r"(?:nav|navbar|navigation|menu|footer|breadcrumbs?|share|sharing|social|cookies?|"
    r"cookie-banner|newsletter|subscribe|pagination|pager|tagcloud|related-posts|comments?)"
    r"(?:[-_](?:wrap|wrapper|container|area|bar|links|list|inner))?$",
    re.I,
)
_LABELS = re.compile(
    r"\s+(?:Amount|Award|Value|Deadline|Due|Location|Posted|Published|Eligibility|Type|"
    r"Country|Level|Funding|Read more|Apply now|Learn more)\b",
    re.I,
)
_LEADING_INDEX = re.compile(r"^\s*(?:#?\d{1,3}[.)]?\s+)(?=\S)")
_GENERIC_ANCHOR = re.compile(
    r"^(read more|learn more|apply( now)?|details|more|view|continue reading|click here|here|"
    r"see more|more info(rmation)?)\W*$",
    re.I,
)
_FILE = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|png|jpe?g|gif|svg|webp|mp4|mp3)$", re.I)
_SOCIAL = re.compile(
    r"(facebook|twitter|x\.com|instagram|youtube|whatsapp|t\.me|pinterest|tiktok|reddit)\.",
    re.I,
)


@dataclass(slots=True)
class ListingAnalysis:
    kind: str
    cards: list[HarvestedCard]
    stats: dict[str, int | float | str] = field(default_factory=dict)


def _text(node: Tag | NavigableString) -> str:
    return (
        " ".join(node.get_text(" ").split())
        if isinstance(node, Tag)
        else " ".join(str(node).split())
    )


def _is_chrome(tag: Tag) -> bool:
    for node in [tag, *tag.parents]:
        if not isinstance(node, Tag):
            continue
        # Page-level classes (WordPress themes put "td-footer-..." on <body>) say nothing about
        # a link; and anything inside <main>/<article> is content.
        if node.name in ("body", "html", "main", "article", "[document]"):
            return False
        if node.name in _CHROME:
            return True
        if node.name == "header" and not node.find_parent(("article", "main")):
            return True
        role = str(node.get("role") or "")
        if role and _CHROME_ROLES.search(role):
            return True
        tokens = [*(node.get("class") or []), str(node.get("id") or "")]
        if any(t and _CHROME_CLASS.match(t) for t in tokens):
            return True
    return False


def _clean_title(raw: str) -> str:
    title = _LEADING_INDEX.sub("", raw).strip()
    cut = _LABELS.search(title)
    if cut and cut.start() >= 8:
        title = title[: cut.start()]
    return title.strip(" -|:–—")[:200]  # noqa: RUF001 - strip en/em dashes


def _title_for(anchor: Tag, card: Tag) -> str:
    for scope in (anchor, card):
        heading = scope.find(re.compile(r"^h[1-6]$"))
        if isinstance(heading, Tag):
            text = _text(heading)
            if len(text) >= 6:
                return _clean_title(text)
    anchor_text = _text(anchor)
    if 6 <= len(anchor_text) <= 140 and not _GENERIC_ANCHOR.match(anchor_text):
        return _clean_title(anchor_text)
    # A whole card wrapped in one anchor: use its first substantial text fragment.
    for fragment in anchor.find_all(string=True):
        text = " ".join(str(fragment).split())
        if len(text) >= 12 and len(text.split()) >= 2 and not text.isdigit():
            return _clean_title(text)
    return _clean_title(anchor_text)


def _signature(tag: Tag) -> str:
    classes = sorted(c for c in (tag.get("class") or []) if not re.search(r"\d", c))[:3]
    return f"{tag.name}.{'.'.join(classes)}"


class ListingSegmenter:
    def analyze(self, soup: BeautifulSoup, page_url: str) -> ListingAnalysis:
        page_host = (urlsplit(page_url).hostname or "").lower()
        page_key = page_url.split("#", 1)[0].rstrip("/")
        candidates: list[tuple[Tag, str]] = []
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = str(anchor["href"]).strip()
            if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            url = urljoin(page_url, href).split("#", 1)[0]
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or url.rstrip("/") == page_key:
                continue
            if _SOCIAL.search(parts.hostname or ""):
                continue
            kind = link_kind(url)
            if kind in (LinkKind.PAGINATION, LinkKind.TAXONOMY) or _FILE.search(parts.path):
                continue
            text = _text(anchor)
            if kind != LinkKind.DETAIL and len(text) < 25:
                continue
            if _is_chrome(anchor):
                continue
            candidates.append((anchor, url))
        candidate_ids = {id(a): url for a, url in candidates}

        def urls_inside(node: Tag) -> set[str]:
            return {
                candidate_ids[id(a)]
                for a in node.find_all("a", href=True)
                if id(a) in candidate_ids
            }

        by_url: dict[str, HarvestedCard] = {}
        signatures: Counter[str] = Counter()
        card_chars = 0
        for anchor, url in candidates:
            card: Tag = anchor
            for parent in anchor.parents:
                if not isinstance(parent, Tag) or parent.name in (
                    "body",
                    "html",
                    "main",
                    "[document]",
                ):
                    break
                if len(urls_inside(parent)) > 1 or len(_text(parent)) > MAX_CARD_TEXT:
                    break
                card = parent
            title = _title_for(anchor, card)
            if len(title) < 10 or len(title.split()) < 2 or _GENERIC_ANCHOR.match(title):
                continue
            body = _text(card)
            snippet = body.replace(title, " ", 1)
            snippet = " ".join(_LEADING_INDEX.sub("", snippet).split())[:280]
            found = HarvestedCard(
                title=title,
                url=url,
                snippet=snippet,
                deadline_snippet=find_deadline_snippet(body),
            )
            prior = by_url.get(url)
            if prior is None:
                signatures[_signature(card)] += 1
                card_chars += len(body)
                by_url[url] = found
            elif len(found.snippet) + len(found.title) > len(prior.snippet) + len(prior.title):
                by_url[url] = found
            if len(by_url) >= MAX_CARDS:
                break
        cards = list(by_url.values())
        repeated = sum(n for n in signatures.values() if n >= 3)
        page_chars = max(1, len(_text(soup.body or soup)))
        offsite = sum(1 for c in cards if (urlsplit(c.url or "").hostname or "") != page_host)
        repeated_share = repeated / len(cards) if cards else 0.0
        card_share = card_chars / page_chars
        is_listing = len(cards) >= MIN_CARDS_FOR_LISTING and (
            repeated_share >= 0.5 or len(cards) >= 12 or card_share >= 0.3
        )
        kind = PageKind.ARTICLE
        if is_listing:
            # A true hub is dominated by uniformly repeated cards; an article with a "related
            # posts" rail is MIXED (its prose is still extracted, the cards still harvested).
            hub = card_share >= 0.25 and repeated_share >= 0.6
            kind = PageKind.LISTING if hub else PageKind.MIXED
        return ListingAnalysis(
            kind=kind,
            cards=cards if is_listing else [],
            stats={
                "candidate_links": len(candidates),
                "cards": len(cards),
                "repeated_structure_share": round(repeated_share, 2),
                "card_text_share": round(card_share, 2),
                "offsite_cards": offsite,
                "with_deadline": sum(1 for c in cards if c.deadline_snippet),
                "kind": kind,
            },
        )
