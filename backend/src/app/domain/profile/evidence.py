"""Personal evidence RAG (pure domain): chunk the student's documents, redact PII before any
model sees a snippet, and check that a synthesized answer is grounded in the evidence.

Constitution: the LLM never receives PII. CVs and transcripts are full of it (name, email,
phone, address, ID numbers), so every snippet passes `redact_pii` (the student's own PII values
plus email / phone / URL / ID-number patterns) before it is put in a prompt or stored as the
evidence behind an answer."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

CHUNK_TARGET_CHARS = 700
CHUNK_MAX_CHARS = 1_100
EVIDENCE_CONFIDENCE = 0.85  # same bar as every other auto-fill
GROUNDING_MIN_OVERLAP = 0.6  # share of the answer's content words found in the cited evidence

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_ID_NUMBER = re.compile(r"\b\d{5}-\d{7}-\d\b|\b[A-Z]{1,2}\d{6,9}\b")  # CNIC / passport-like
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*", re.I)
_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "with",
        "you",
        "your",
        "me",
        "am",
        "also",
        "very",
    }
)
REDACTED = "[redacted]"


def chunk_document(text: str) -> list[str]:
    """Line/paragraph-aligned chunks of ~700 chars. CV sections (Education, Skills, Projects...)
    stay together where possible so a retrieved chunk carries its own context."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) <= 1:
        blocks = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        while len(block) > CHUNK_MAX_CHARS:
            cut = block.rfind(" ", 0, CHUNK_MAX_CHARS)
            cut = cut if cut > CHUNK_TARGET_CHARS // 2 else CHUNK_MAX_CHARS
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block[:cut].strip())
            block = block[cut:].strip()
        if current and len(current) + len(block) + 1 > CHUNK_TARGET_CHARS:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if len(c) >= 20]


def redact_pii(text: str, known_values: Iterable[str] = ()) -> str:
    """Remove the student's own PII values (exact, case-insensitive) and generic PII patterns."""
    out = text
    for value in sorted(
        {v.strip() for v in known_values if v and len(v.strip()) >= 3}, key=len, reverse=True
    ):
        out = re.sub(re.escape(value), REDACTED, out, flags=re.I)
    out = _EMAIL.sub(REDACTED, out)
    out = _URL.sub(REDACTED, out)
    out = _ID_NUMBER.sub(REDACTED, out)
    out = _PHONE.sub(REDACTED, out)
    return out


def content_words(text: str) -> list[str]:
    return [w.lower().strip(".") for w in _WORD.findall(text) if w.lower() not in _STOP]


def grounding_overlap(answer: str, evidence: Sequence[str]) -> float:
    """Share of the answer's content words that appear in the evidence (0..1)."""
    words = content_words(answer)
    if not words:
        return 0.0
    haystack = set(content_words(" ".join(evidence)))
    return sum(1 for w in words if w in haystack) / len(words)


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    source: str  # DOCUMENT | ESSAY | MEMORY
    text: str  # already PII-redacted
    similarity: float
    ref_id: str | None = None  # document id / memory id


@dataclass(frozen=True, slots=True)
class EvidenceAnswer:
    answer: str | None
    confidence: float  # combined: model confidence x grounding
    source: str | None  # where the cited evidence came from
    snippet: str | None  # the (redacted) evidence the answer is grounded in
    grounded: bool
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.answer is not None and self.grounded and self.confidence >= EVIDENCE_CONFIDENCE


def combine_confidence(model_confidence: float, overlap: float) -> float:
    """A model's self-reported confidence counts only when the answer is grounded: below the
    overlap bar it is halved, so a confident hallucination can never reach the auto-fill bar."""
    model_confidence = max(0.0, min(1.0, model_confidence))
    if overlap >= GROUNDING_MIN_OVERLAP:
        return round(model_confidence, 4)
    return round(model_confidence * 0.5, 4)
