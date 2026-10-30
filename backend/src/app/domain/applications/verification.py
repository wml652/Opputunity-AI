"""Final verification gate (pure domain).

Nothing is submitted without a recorded human OK whose `preview_hash` matches:

1. The user reviews the staged application (screenshot + every field value) and approves the
   preview they saw, identified by `preview_hash = sha256(canonical field values)`.
2. The server recomputes the hash from the stored values; a stale preview is rejected.
3. Edits are applied and the APPROVED hash is computed over the final values.
4. The submitter replays the portal with the approved values, reads every control back from the
   page, recomputes the hash over what the page really shows, and clicks Submit only when it
   equals the approved hash. Any mismatch re-stages the application for review."""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PreviewValue:
    step_index: int
    field_key: str
    value: str | None


def normalize_value(value: str | None) -> str:
    """Whitespace/line-ending insensitive (browsers turn \\n into \\r\\n in textareas)."""
    if value is None:
        return ""
    return " ".join(value.replace("\r\n", "\n").split())


def compute_preview_hash(values: Iterable[PreviewValue]) -> str:
    canonical = sorted((v.step_index, v.field_key, normalize_value(v.value)) for v in values)
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class VerificationOutcome(enum.StrEnum):
    APPROVED = "APPROVED"  # human OK recorded, submission pending
    REJECTED = "REJECTED"  # human said no: application cancelled
    SUBMITTED = "SUBMITTED"  # submitted, receipt captured
    RESTAGED = "RESTAGED"  # on-page values differed from the approval: back to review
    FAILED = "FAILED"  # submission failed (never retried automatically)


@dataclass(slots=True)
class FinalVerification:
    id: uuid.UUID
    user_id: uuid.UUID
    application_id: uuid.UUID
    checkpoint_id: uuid.UUID | None
    preview_hash_seen: str
    approved_preview_hash: str | None
    edited_fields: list[str]
    outcome: VerificationOutcome
    created_at: datetime
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    reject_reason: str | None = None
    submitted_at: datetime | None = None
    observed_hash: str | None = None
    final_url: str | None = None
    error: str | None = None
    mismatches: list[dict[str, str]] = field(default_factory=list)
    has_receipt: bool = False


class SubmitOutcomeKind(enum.StrEnum):
    SUBMITTED = "SUBMITTED"
    MISMATCH = "MISMATCH"  # page values differ from the approved ones: NOT submitted
    BLOCKED = "BLOCKED"  # CAPTCHA / login / no submit button: NOT submitted
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    kind: SubmitOutcomeKind
    observed_hash: str | None = None
    mismatches: list[dict[str, str]] = field(default_factory=list)
    receipt_png: bytes | None = None
    receipt_text: str = ""
    final_url: str | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)
