"""Reference essays (the student's authentic writing) and their stylometric fingerprint."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.shared.errors import ValidationError

MAX_REFERENCE_ESSAYS = 5
RECOMMENDED_ESSAYS = 4
MIN_ESSAY_WORDS = 50
MAX_ESSAY_CHARS = 30_000
CHUNK_TARGET_WORDS = 160

_WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z]+)?")


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


@dataclass(slots=True)
class ReferenceEssay:
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    body: str
    prompt_context: str | None
    word_count: int
    created_at: datetime
    updated_at: datetime

    def validate(self) -> None:
        if not self.title.strip():
            raise ValidationError("title is required")
        if len(self.body) > MAX_ESSAY_CHARS:
            raise ValidationError(f"essay exceeds {MAX_ESSAY_CHARS} characters")
        if self.word_count < MIN_ESSAY_WORDS:
            raise ValidationError(f"essay must have at least {MIN_ESSAY_WORDS} words")


def chunk_essay(body: str, target_words: int = CHUNK_TARGET_WORDS) -> list[str]:
    """Paragraph-aligned chunks of roughly `target_words` for few-shot retrieval. Paragraphs are
    never split unless a single paragraph is more than twice the target."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for para in paragraphs:
        words = word_count(para)
        if words > target_words * 2:
            if current:
                chunks.append("\n\n".join(current))
                current, count = [], 0
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf: list[str] = []
            n = 0
            for sentence in sentences:
                buf.append(sentence)
                n += word_count(sentence)
                if n >= target_words:
                    chunks.append(" ".join(buf))
                    buf, n = [], 0
            if buf:
                chunks.append(" ".join(buf))
            continue
        if current and count + words > target_words * 1.5:
            chunks.append("\n\n".join(current))
            current, count = [], 0
        current.append(para)
        count += words
    if current:
        chunks.append("\n\n".join(current))
    return chunks


@dataclass(frozen=True, slots=True)
class StyleAnalysis:
    """Output of the stylometry engine: human-readable metrics plus a normalized vector."""

    metrics: dict[str, Any]
    feature_names: list[str]
    feature_vector: list[float]


@dataclass(slots=True)
class StyleFingerprint:
    id: uuid.UUID
    user_id: uuid.UUID
    version: int
    essay_count: int
    total_words: int
    metrics: dict[str, Any]
    feature_names: list[str] = field(default_factory=list)
    feature_vector: list[float] = field(default_factory=list)
    created_at: datetime | None = None
