"""Self-learning Q&A memory: answers the student gave to custom portal questions, recalled by
semantic similarity so the same (or a paraphrased) question is never asked twice."""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.shared.errors import ValidationError

AUTO_FILL_THRESHOLD = 0.85
MAX_QUESTION_CHARS = 2_000
MAX_ANSWER_CHARS = 10_000


class MemorySource(enum.StrEnum):
    USER = "USER"  # typed by the student (inbox answer or vault edit)
    PROFILE = "PROFILE"  # derived from the profile
    INFERRED = "INFERRED"  # proposed by the agent, confirmed later


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """Cosine similarity in [0, 1] compared against the auto-fill threshold."""

    value: float
    threshold: float = AUTO_FILL_THRESHOLD

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValidationError("threshold must be within [0, 1]")

    @staticmethod
    def from_cosine(similarity: float, threshold: float = AUTO_FILL_THRESHOLD) -> ConfidenceScore:
        return ConfidenceScore(value=max(0.0, min(1.0, similarity)), threshold=threshold)

    @property
    def auto_fill(self) -> bool:
        return self.value >= self.threshold


_SPACE = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s?.!:*]+$")


def normalize_question(text: str) -> str:
    """Canonical form for exact-duplicate detection (semantic matching uses embeddings)."""
    collapsed = _SPACE.sub(" ", text.strip().lower())
    return _TRAILING.sub("", collapsed)


@dataclass(slots=True)
class KnowledgeQA:
    id: uuid.UUID
    user_id: uuid.UUID
    question_text: str
    question_normalized: str
    answer_text: str
    embedding_model: str
    source: MemorySource
    times_reused: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def validate(self) -> None:
        if not self.question_normalized:
            raise ValidationError("question is required")
        if len(self.question_text) > MAX_QUESTION_CHARS:
            raise ValidationError(f"question exceeds {MAX_QUESTION_CHARS} characters")
        if not self.answer_text.strip():
            raise ValidationError("answer is required")
        if len(self.answer_text) > MAX_ANSWER_CHARS:
            raise ValidationError(f"answer exceeds {MAX_ANSWER_CHARS} characters")


@dataclass(frozen=True, slots=True)
class RecallMatch:
    qa: KnowledgeQA
    confidence: ConfidenceScore


@dataclass(frozen=True, slots=True)
class RecallResult:
    query: str
    embedding_model: str
    threshold: float
    matches: list[RecallMatch]
    searched: int  # memory rows comparable with the current embedding model
    stale: int  # rows embedded with a different model (not comparable)

    @property
    def best(self) -> RecallMatch | None:
        return self.matches[0] if self.matches else None

    @property
    def auto_fill(self) -> bool:
        return self.best is not None and self.best.confidence.auto_fill
