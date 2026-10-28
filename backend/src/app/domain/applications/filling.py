"""Form filling vocabulary (pure): portal fields, how each was resolved, escalated questions,
browser checkpoints, and what one filling run produced."""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.memory.models import AUTO_FILL_THRESHOLD
from app.domain.shared.enums import QuestionStatus


class FieldSource(enum.StrEnum):
    PROFILE = "PROFILE"
    DOCUMENT = "DOCUMENT"
    ESSAY = "ESSAY"
    MEMORY = "MEMORY"
    USER = "USER"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class FormField:
    """A visible, fillable control discovered on the current portal step."""

    key: str  # stable name/id attribute
    label: str
    field_type: str  # text|email|tel|number|date|url|textarea|select|radio|checkbox|file
    selector: str
    options: list[str] = field(default_factory=list)
    required: bool = False
    max_length: int | None = None
    accept: str | None = None  # file inputs


@dataclass(frozen=True, slots=True)
class FieldResolution:
    key: str
    value: str | None
    source: FieldSource
    confidence: float
    memory_qa_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    essay_draft_id: uuid.UUID | None = None
    suggestion: str | None = None  # best guess shown to the user when escalating
    note: str = ""

    @property
    def fillable(self) -> bool:
        return self.value is not None and self.confidence >= AUTO_FILL_THRESHOLD


def dom_fingerprint(fields: list[FormField]) -> str:
    """Identity of a portal step: which controls it has (not their values)."""
    signature = "|".join(sorted(f"{f.key}:{f.field_type}" for f in fields))
    return hashlib.sha256(signature.encode()).hexdigest()[:32]


@dataclass(slots=True)
class ApplicationField:
    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID
    step_index: int
    field_key: str
    label: str
    field_type: str
    options: list[str]
    required: bool
    value: str | None  # plaintext in the domain; encrypted at rest
    source: FieldSource
    confidence: float
    memory_qa_id: uuid.UUID | None
    document_id: uuid.UUID | None
    essay_draft_id: uuid.UUID | None
    user_edited: bool
    updated_at: datetime


@dataclass(slots=True)
class PendingQuestion:
    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID
    field_key: str
    step_index: int
    question_text: str
    context: str
    field_type: str
    options: list[str]
    suggested_answer: str | None
    confidence: float
    status: QuestionStatus
    answered_qa_id: uuid.UUID | None
    created_at: datetime
    answered_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CheckpointData:
    step_index: int
    url: str
    dom_fingerprint: str
    storage_state: str  # JSON from Playwright context.storage_state(); encrypted at rest
    screenshot_png: bytes | None
    fill_ledger: list[dict[str, Any]]


@dataclass(slots=True)
class Checkpoint:
    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID
    seq: int
    step_index: int
    url: str
    dom_fingerprint: str
    fill_ledger: list[dict[str, Any]]
    has_screenshot: bool
    created_at: datetime


class FillOutcomeKind(enum.StrEnum):
    PAUSED = "PAUSED"  # unknown fields on this step; checkpoint taken
    READY_TO_SUBMIT = "READY_TO_SUBMIT"  # reached the final step; never clicks Submit
    BLOCKED = "BLOCKED"  # captcha / login / navigation impossible
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class UnresolvedField:
    field: FormField
    resolution: FieldResolution
    step_index: int
    context: str


@dataclass(frozen=True, slots=True)
class FillOutcome:
    kind: FillOutcomeKind
    step_index: int
    checkpoint: CheckpointData | None
    unresolved: list[UnresolvedField] = field(default_factory=list)
    replayed: bool = (
        False  # the portal lost its draft; earlier steps were re-filled from the ledger
    )
    steps_completed: int = 0
    error: str | None = None
    log: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResumeState:
    url: str
    step_index: int
    dom_fingerprint: str
    storage_state: str


@dataclass(frozen=True, slots=True)
class DocumentBlob:
    document_id: uuid.UUID
    filename: str
    data: bytes
