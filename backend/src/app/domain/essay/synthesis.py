"""Stage 2 essay synthesis vocabulary and deterministic scores (pure)."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.shared.enums import EssayStatus

CLICHES: tuple[str, ...] = (
    "delve",
    "delving",
    "tapestry",
    "testament to",
    "moreover",
    "unwavering",
    "beacon",
    "in today's fast-paced world",
    "ever-evolving",
    "embark on a journey",
    "journey of",
    "navigate the complexities",
    "foster a sense",
    "a myriad of",
    "plethora",
    "pivotal",
    "realm of",
    "resonate with",
    "ignite my passion",
    "fueled my passion",
    "multifaceted",
    "underscore",
    "nestled",
    "in conclusion",
    "furthermore",
    "it is important to note",
    "showcasing",
    "intricate",
    "seamlessly",
    "transformative",
    "elevate",
)
_CLICHE_RES = [(c, re.compile(rf"(?<![A-Za-z]){re.escape(c)}(?![A-Za-z])", re.I)) for c in CLICHES]


def find_cliches(text: str) -> list[str]:
    return [c for c, rx in _CLICHE_RES if rx.search(text)]


def voice_match_score(student: Sequence[float], draft: Sequence[float]) -> float:
    """Cosine similarity of the normalized stylometric feature vectors, in [0, 1]."""
    if not student or len(student) != len(draft):
        return 0.0
    dot = sum(a * b for a, b in zip(student, draft, strict=True))
    norm = math.sqrt(sum(a * a for a in student)) * math.sqrt(sum(b * b for b in draft))
    return round(max(0.0, min(1.0, dot / norm)), 4) if norm else 0.0


def burstiness_score(cv_student: float, cv_draft: float) -> float:
    """1 - |CV_draft - CV_student| / CV_student, clamped to [0, 1]."""
    if cv_student <= 0:
        return 0.0
    return round(max(0.0, min(1.0, 1 - abs(cv_draft - cv_student) / cv_student)), 4)


def word_limit_ok(text: str, limit: int | None) -> bool:
    if not limit:
        return True
    return len(re.findall(r"[A-Za-z0-9']+", text)) <= limit


@dataclass(slots=True)
class EssayDraft:
    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    field_key: str | None
    prompt: str
    word_limit: int | None
    body: str
    critique: dict[str, Any] = field(default_factory=dict)
    voice_match_score: float | None = None
    burstiness_score: float | None = None
    status: EssayStatus = EssayStatus.DRAFT
    requires_manual: bool = False  # the opportunity prohibits AI-written essays
    model: str | None = None
    fingerprint_version: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
