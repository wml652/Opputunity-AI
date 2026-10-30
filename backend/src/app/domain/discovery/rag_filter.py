"""In-page retrieval: send the LLM only the parts of a long page that describe opportunities.

A 20,000-character detail page is mostly navigation, related posts, comments, cookie text and
newsletter boxes. Instead of extracting every window, the distilled Markdown is split into
semantic sections (headings, tables, card groups), each section is scored against an extraction
intent (plus the campaign keywords) and only the top-scoring sections are kept, in their original
order. When the kept text fits one window (<= 6,000 chars) the page needs a single LLM call.

Stage A (always, local, deterministic): BM25 over sections + opportunity-signal boosters (dates,
apply, eligibility, GPA, stipend, degree words...) + optional embedding cosine.
Stage B (optional): a user-configured cross-encoder reranker; its scores are blended in.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

INTENT_TERMS = (
    "scholarship",
    "fellowship",
    "internship",
    "research",
    "grant",
    "award",
    "program",
    "programme",
    "apply",
    "application",
    "deadline",
    "eligibility",
    "eligible",
    "requirements",
    "criteria",
    "gpa",
    "stipend",
    "funding",
    "funded",
    "tuition",
    "undergraduate",
    "bachelor",
    "master",
    "phd",
    "students",
    "nationality",
    "benefits",
    "duration",
    "how to apply",
)
INTENT_QUERY = (
    "opportunity details: title, organization, deadline, eligibility criteria, degree level, GPA, "
    "nationality, funding, stipend, benefits, how to apply, application portal"
)
_SIGNAL_PATTERNS = (
    re.compile(r"\b(deadline|closing date|apply by|due date)\b", re.I),
    re.compile(r"\b(eligib\w*|requirement\w*|criteria|who can apply)\b", re.I),
    re.compile(r"\b(gpa|cgpa|grade point)\b", re.I),
    re.compile(r"\b(stipend|allowance|tuition|fully funded|funding|salary|paid)\b", re.I),
    re.compile(r"\b(undergraduate|bachelor'?s?|master'?s?|phd|doctoral|graduate)\b", re.I),
    re.compile(
        r"\b(apply now|how to apply|application (form|portal|process)|apply online)\b", re.I
    ),
    re.compile(
        r"\b(\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+20\d\d|"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+20\d\d|"
        r"20\d\d-\d\d-\d\d)\b",
        re.I,
    ),
)
_BOILERPLATE = re.compile(
    r"\b(cookie|privacy policy|terms of (use|service)|subscribe|newsletter|sign up for|"
    r"share (this|on)|follow us|all rights reserved|related posts?|you may also like|"
    r"leave a (reply|comment)|comments?\b|advertisement|whatsapp channel|telegram)\b",
    re.I,
)
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_TOKEN = re.compile(r"[a-z0-9]+")

SECTION_JOINER = "\n\n"
SINGLE_WINDOW_CHARS = 6_000
MIN_FILTER_CHARS = 6_000  # shorter pages already fit one window: never filtered
MIN_SECTION_SCORE = 0.08  # below this (relative to the best section) it is boilerplate
RELEVANT_RATIO = 0.3  # kept even past one window: clearly about the opportunity
RELEVANT_SIGNALS = 0.28  # two of seven kinds of opportunity signal (deadline + eligibility, ...)


@dataclass(frozen=True, slots=True)
class Section:
    index: int
    heading: str
    text: str
    kind: str  # heading | table | cards | text

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class ScoredSection:
    section: Section
    score: float  # final blended score in [0, 1]
    bm25: float
    signals: float
    cosine: float | None = None
    rerank: float | None = None


@dataclass(frozen=True, slots=True)
class RagSelection:
    text: str
    sections_total: int
    sections_kept: int
    chars_before: int
    chars_after: int
    filtered: bool
    kept: list[ScoredSection]
    dropped: list[ScoredSection]

    @property
    def chars_saved_pct(self) -> float:
        if self.chars_before == 0:
            return 0.0
        return round(100.0 * (1 - self.chars_after / self.chars_before), 1)

    @property
    def single_window(self) -> bool:
        return self.chars_after <= SINGLE_WINDOW_CHARS


def split_sections(markdown: str, max_section_chars: int = 2_500) -> list[Section]:
    """Heading-delimited sections; tables and runs of list/card lines become their own sections;
    very long sections are cut at paragraph boundaries so a single giant block cannot defeat the
    filter."""
    sections: list[Section] = []
    heading = ""
    buf: list[str] = []
    block_kind = "text"

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        buf = []
        if not text:
            return
        for part in _cut(text, max_section_chars):
            sections.append(Section(len(sections), heading, part, block_kind))

    for raw in markdown.splitlines():
        line = raw.rstrip()
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(2).strip()[:200]
            block_kind = "heading"
            buf.append(line)
            continue
        kind = (
            "table"
            if line.lstrip().startswith("|")
            else "cards"
            if line.lstrip().startswith(("- [", "* [", "- ", "* "))
            else "text"
        )
        if kind in ("table", "cards") and block_kind not in (kind, "heading") and buf:
            flush()
        if kind == "text" and block_kind in ("table", "cards") and line.strip() and buf:
            flush()
        if line.strip() or buf:
            if not buf:
                block_kind = kind if kind != "text" else ("heading" if heading else "text")
            buf.append(line)
    flush()
    return sections


def _cut(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) + 2 > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > limit:
            parts.append(current[:limit])
            current = current[limit:]
    if current.strip():
        parts.append(current)
    return parts


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def query_terms(keywords: Sequence[str]) -> list[str]:
    terms: list[str] = list(INTENT_TERMS)
    for kw in keywords:
        terms += [t for t in tokens(kw) if len(t) > 2]
    # Short tokens ("to", "a" from "how to apply") match every section: never query terms.
    return list(dict.fromkeys(t for term in terms for t in tokens(term) if len(t) > 2))


def bm25_scores(
    sections: Sequence[Section], terms: Sequence[str], k1: float = 1.4, b: float = 0.75
) -> list[float]:
    docs = [Counter(tokens(f"{s.heading} {s.text}")) for s in sections]
    lengths = [sum(d.values()) or 1 for d in docs]
    avg = sum(lengths) / max(1, len(lengths))
    n = len(docs)
    scores: list[float] = []
    for doc, length in zip(docs, lengths, strict=True):
        score = 0.0
        for term in set(terms):
            tf = doc.get(term, 0)
            if not tf:
                continue
            df = sum(1 for d in docs if term in d)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        scores.append(score)
    return scores


def signal_score(section: Section) -> float:
    """Opportunity evidence in [0, 1]: each kind of signal counts once; boilerplate penalized."""
    text = f"{section.heading}\n{section.text}"
    hits = sum(1 for p in _SIGNAL_PATTERNS if p.search(text))
    boiler = len(_BOILERPLATE.findall(text))
    score = hits / len(_SIGNAL_PATTERNS)
    if boiler:
        score -= min(0.5, 0.15 * boiler) * (1.0 if hits <= 2 else 0.4)
    if section.kind == "table" and hits:
        score += 0.1  # deadline calendars / eligibility tables
    return max(0.0, min(1.0, score))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def boilerplate_factor(section: Section, signals: float) -> float:
    """Comments, related posts, newsletters, cookie banners: down-weighted unless the section
    also carries clear opportunity signals."""
    hits = len(_BOILERPLATE.findall(f"{section.heading}\n{section.text}"))
    if not hits or signals >= RELEVANT_SIGNALS:
        return 1.0
    return max(0.25, 1.0 - 0.3 * hits)


def _normalize(values: Sequence[float]) -> list[float]:
    hi = max(values, default=0.0)
    return [v / hi if hi > 0 else 0.0 for v in values]


def score_sections(
    sections: Sequence[Section],
    keywords: Sequence[str],
    *,
    cosines: Sequence[float] | None = None,
    rerank: Sequence[float] | None = None,
) -> list[ScoredSection]:
    """Stage A (BM25 + signals + optional cosine) blended with optional Stage B rerank scores
    (already in [0, 1]); a reranker, when configured, carries the most weight."""
    if not sections:
        return []
    bm = _normalize(bm25_scores(sections, query_terms(keywords)))
    sig = [signal_score(s) for s in sections]
    cos = _normalize([max(0.0, c) for c in cosines]) if cosines else None
    out: list[ScoredSection] = []
    for i, section in enumerate(sections):
        stage_a = 0.5 * bm[i] + 0.35 * sig[i] + (0.15 * cos[i] if cos else 0.15 * bm[i])
        score = stage_a if rerank is None else 0.35 * stage_a + 0.65 * rerank[i]
        score *= boilerplate_factor(section, sig[i])
        out.append(
            ScoredSection(
                section,
                round(score, 4),
                round(bm[i], 4),
                round(sig[i], 4),
                round(cos[i], 4) if cos else None,
                round(rerank[i], 4) if rerank is not None else None,
            )
        )
    return out


def select_sections(
    scored: Sequence[ScoredSection],
    *,
    budget_chars: int = SINGLE_WINDOW_CHARS,
    max_chars: int = 40_000,
) -> RagSelection:
    """Keep (in page order):
    1. the lead section (pages open with the opportunity's name and summary) unless it is pure
       boilerplate;
    2. every clearly relevant section - relative score >= 0.3 or at least two kinds of
       opportunity signal - so a page listing many opportunities keeps all of them (up to
       `max_chars`);
    3. then the next best sections while the text still fits one extraction window.
    Everything else (navigation, related posts, comments, newsletters...) is dropped."""
    total = sum(s.section.chars for s in scored)
    if total <= MIN_FILTER_CHARS or not scored:
        return RagSelection(
            text=SECTION_JOINER.join(s.section.text for s in scored),
            sections_total=len(scored),
            sections_kept=len(scored),
            chars_before=total,
            chars_after=total,
            filtered=False,
            kept=list(scored),
            dropped=[],
        )
    best = max(s.score for s in scored) or 1.0
    keep: dict[int, ScoredSection] = {}
    used = 0

    def add(s: ScoredSection, limit: int) -> None:
        nonlocal used
        if s.section.index not in keep and used + s.section.chars <= limit:
            keep[s.section.index] = s
            used += s.section.chars

    lead = scored[0]
    if lead.score / best >= MIN_SECTION_SCORE:
        add(lead, max_chars)
    ranked = sorted(scored, key=lambda s: -s.score)
    for s in ranked:
        relevant = s.score / best >= RELEVANT_RATIO or s.signals >= RELEVANT_SIGNALS
        if relevant:
            add(s, max_chars)
    for s in ranked:
        if s.score / best >= MIN_SECTION_SCORE:
            add(s, budget_chars)
    if not keep:
        add(ranked[0], max(max_chars, ranked[0].section.chars))
    ordered = [s for s in scored if s.section.index in keep]
    return RagSelection(
        text=SECTION_JOINER.join(s.section.text for s in ordered),
        sections_total=len(scored),
        sections_kept=len(ordered),
        chars_before=total,
        chars_after=sum(s.section.chars for s in ordered),
        filtered=True,
        kept=ordered,
        dropped=[s for s in scored if s.section.index not in keep],
    )
