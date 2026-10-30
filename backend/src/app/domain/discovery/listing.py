"""Hub / listing pages and near-duplicate detection (pure domain, stdlib only).

A *hub* (directory, category page, listicle, deadline calendar) lists many opportunities as
cards that link to detail pages. Discovery harvests those cards deterministically, scores the
links, and spends its crawl budget on the detail pages instead of asking the LLM to squeeze
dozens of half-described items out of the hub's Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

from app.domain.discovery.models import registrable_host

# ---- harvested cards ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarvestedCard:
    """One listing card / table row: its title, the link it points to, and nearby text."""

    title: str
    url: str | None
    snippet: str = ""
    deadline_snippet: str | None = None

    def to_markdown(self) -> str:
        head = f"[{self.title}]({self.url})" if self.url else self.title
        parts = [head]
        if self.deadline_snippet:
            parts.append(f"Deadline: {self.deadline_snippet}")
        if self.snippet:
            parts.append(self.snippet)
        return "- " + " | ".join(parts)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "deadline_snippet": self.deadline_snippet,
        }


class PageKind:
    ARTICLE = "article"  # prose / single opportunity detail page
    LISTING = "listing"  # directory, category page, listicle of linked cards
    MIXED = "mixed"  # prose plus a card grid or calendar tables


_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{4}})\b",
    re.I,
)
_DEADLINE_HINT = re.compile(r"(deadline|closes?|closing date|apply by|due)\s*[:\-]?\s*", re.I)


def find_deadline_snippet(text: str) -> str | None:
    """The date phrase that most likely is the deadline (prefers one labelled as such)."""
    hint = _DEADLINE_HINT.search(text)
    if hint:
        near = _DATE.search(text, hint.end(), hint.end() + 60)
        if near:
            return near.group(0)
    match = _DATE.search(text)
    return match.group(0) if match else None


# ---- link classification & scoring --------------------------------------------------------------

_PAGINATION = re.compile(r"(?:[?&](?:page|paged|p|start|offset)=\d+|/page/\d+/?$)", re.I)
_TAXONOMY = re.compile(
    r"/(?:tag|tags|author|authors|category|categories|topic|topics|archive|archives|label|"
    r"search|feed|wp-json|wp-admin|login|signin|signup|register|cart|account|privacy|terms|"
    r"cookie|contact|about|advertise|subscribe|newsletter|share)(?:/|$)",
    re.I,
)
_SLUG_WORD = re.compile(r"[a-z0-9]+", re.I)
_OPPORTUNITY_TOKENS = (
    "scholarship",
    "fellowship",
    "internship",
    "intern",
    "grant",
    "award",
    "bursary",
    "program",
    "programme",
    "research",
    "admission",
    "apply",
    "application",
    "funded",
    "exchange",
    "summer",
    "school",
    "competition",
    "challenge",
    "job",
    "vacanc",
    "position",
    "call",
)
_OFFICIAL_SUFFIXES = (".edu", ".gov", ".ac.", ".edu.", ".gov.", ".int", ".org")
_SOCIAL = re.compile(
    r"(facebook|twitter|x\.com|instagram|linkedin\.com/(?!jobs)|youtube|whatsapp|t\.me|"
    r"pinterest|tiktok|reddit)\b",
    re.I,
)


class LinkKind:
    PAGINATION = "pagination"
    TAXONOMY = "taxonomy"
    DETAIL = "detail"
    OTHER = "other"


def link_kind(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if _PAGINATION.search(path + (f"?{parts.query}" if parts.query else "")):
        return LinkKind.PAGINATION
    if _TAXONOMY.search(path):
        return LinkKind.TAXONOMY
    last = [seg for seg in path.split("/") if seg][-1:] or [""]
    words = _SLUG_WORD.findall(last[0])
    if len(words) >= 3 or (len(words) >= 2 and any(ch.isdigit() for ch in last[0])):
        return LinkKind.DETAIL
    return LinkKind.OTHER


def detail_link_score(
    url: str, anchor: str, keywords: list[str] | tuple[str, ...], hub_url: str
) -> int:
    """Higher = better detail-page candidate. <= 0 means do not follow.

    Penalizes pagination indices (`/page/38/`), taxonomy pages (`/tag/`, `/author/`, bare
    `/category/x/`), social links; boosts opportunity slugs, keyword matches, dates in the
    anchor and off-site official domains (.edu/.gov/.ac.*/.org program pages)."""
    if _SOCIAL.search(url):
        return -10
    kind = link_kind(url)
    hay = f"{url} {anchor}".lower()
    score = 0
    if kind == LinkKind.PAGINATION:
        # Only the first couple of index pages are worth a visit (newest listings).
        page = re.search(r"(?:/page/|[?&](?:page|paged|p)=)(\d+)", url, re.I)
        return 2 if page and int(page.group(1)) <= 2 else -5
    if kind == LinkKind.TAXONOMY:
        score -= 4
    if kind == LinkKind.DETAIL:
        score += 4
    score += 2 * sum(1 for token in _OPPORTUNITY_TOKENS if token in hay)
    for kw in keywords:
        tokens = [t for t in re.split(r"\W+", kw.lower()) if len(t) > 2]
        if tokens and all(t in hay for t in tokens):
            score += 4
        elif tokens and any(t in hay for t in tokens):
            score += 2
    if re.search(r"20[2-3]\d", hay):
        score += 1
    host = (urlsplit(url).hostname or "").lower()
    if registrable_host(url) != registrable_host(hub_url):
        if any(s in host or host.endswith(s.rstrip(".")) for s in _OFFICIAL_SUFFIXES):
            score += 3  # the official program page behind a listicle
        else:
            score -= 1
    if len(anchor.strip()) < 4:
        score -= 2
    return score


# ---- near-duplicate detection -------------------------------------------------------------------

FUZZY_DUPLICATE_RATIO = 0.85
_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
        "in",
        "at",
        "to",
        "on",
        "with",
        "by",
        "program",
        "programme",
        "fully",
        "funded",
        "paid",
        "open",
        "now",
        "apply",
        "online",
    }
)
_YEARISH = re.compile(r"\b(?:20\d\d)(?:\s*[-/]\s*(?:20)?\d\d)?\b")


def _stem(token: str) -> str:
    return (
        token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token
    )


def title_tokens(title: str, organization: str | None = None) -> frozenset[str]:
    text = f"{title} {organization or ''}".lower()
    text = _YEARISH.sub(" ", text)
    return frozenset(
        _stem(t)
        for t in re.findall(r"[a-z0-9]+", text)
        if t not in _STOP and (len(t) > 1 or t.isdigit())
    )


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = "&".join(
        q
        for q in parts.query.split("&")
        if q and not q.lower().startswith(("utm_", "ref=", "fbclid"))
    )
    return urlunsplit(("https", host, path, query, ""))


def has_distinct_identifiers(a: frozenset[str] | str, b: frozenset[str] | str) -> bool:
    """True when two titles differ in a numeric token ("Zeta 1" vs "Zeta 2", "Grant 01" vs
    "Grant 02"): such titles name DIFFERENT opportunities, however similar the rest is. Years are
    stripped first, so "2026" vs "2027" editions are not treated as distinct here."""
    ta = a if isinstance(a, frozenset) else title_tokens(a)
    tb = b if isinstance(b, frozenset) else title_tokens(b)
    return any(any(ch.isdigit() for ch in t) for t in ta ^ tb)


def fuzzy_similarity(title_a: str, org_a: str | None, title_b: str, org_b: str | None) -> float:
    """Similarity of two normalized titles (+ organization) in [0, 1]: the max of token-set
    Jaccard, containment (one title's tokens inside the other's, discounted for short titles),
    and a character SequenceMatcher ratio that only counts once the token sets mostly agree (so
    "UNHCR Internships" never matches "UNICEF Internships")."""
    ta, tb = title_tokens(title_a, org_a), title_tokens(title_b, org_b)
    if not ta or not tb:
        return 0.0
    if has_distinct_identifiers(ta, tb):
        # "Zeta 1 Internship" vs "Zeta 2 Internship", "Grant 01" vs "Grant 02": a differing
        # number (years were already stripped) identifies a different opportunity.
        return 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    small = min(len(ta), len(tb))
    containment = inter / small
    if small == 3:
        containment *= 0.9
    elif small < 3:
        containment = min(containment, jaccard + 0.2)
    seq = (
        SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
        if jaccard >= 0.5
        else 0.0
    )
    return max(jaccard, containment, seq)


@dataclass(frozen=True, slots=True)
class DedupeCandidate:
    title: str
    organization: str | None
    portal_url: str | None


def is_near_duplicate(
    a: DedupeCandidate, b: DedupeCandidate, threshold: float = FUZZY_DUPLICATE_RATIO
) -> bool:
    """Same opportunity described twice (overlapping extraction windows, two listicles...).

    Same canonical portal URL counts only when that URL is a specific page (not a site root /
    listing page that many cards share)."""
    ca, cb = canonical_url(a.portal_url), canonical_url(b.portal_url)
    same_page = ca is not None and ca == cb and urlsplit(ca).path not in ("", "/")
    if same_page and fuzzy_similarity(a.title, None, b.title, None) >= 0.7:
        return True
    if a.organization and b.organization:
        oa, ob = title_tokens(a.organization), title_tokens(b.organization)
        org_ratio = SequenceMatcher(None, a.organization.lower(), b.organization.lower()).ratio()
        if not (oa & ob) and org_ratio < 0.8:
            return False  # clearly different organizations
    score = fuzzy_similarity(a.title, a.organization, b.title, b.organization)
    if min(len(title_tokens(a.title)), len(title_tokens(b.title))) >= 3:
        # Organizations are compatible (checked above); differently-worded org names must not
        # dilute two specific, matching titles.
        score = max(score, fuzzy_similarity(a.title, None, b.title, None))
    return score >= threshold
