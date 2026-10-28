"""Resolve portal form fields in strict priority order:

0. values already settled for this application (the student's own answers, earlier steps)
1. deterministic Student Profile synonyms (name, email, phone, address, GPA, degree, ...)
2. document upload slots (CV, transcript, ID, certificate)
3. essay prompts (long textareas / essay-like labels) -> Stage 2 essay engine
4. semantic Q&A memory (cosine similarity against learned answers)

A field is auto-filled only with confidence >= 0.85; required fields below that are escalated to
the student, optional ones are left blank. Choice fields are only ever set to an option the
portal actually offers.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.domain.applications.filling import FieldResolution, FieldSource, FormField
from app.domain.essay.synthesis import EssayDraft
from app.domain.memory.models import AUTO_FILL_THRESHOLD, RecallResult
from app.domain.shared.enums import DocKind

PROFILE_CONFIDENCE = 0.97

# (phrases, fact key). More specific phrases first; the longest matching phrase wins.
SYNONYMS: list[tuple[tuple[str, ...], str]] = [
    (("first name", "given name", "forename"), "first_name"),
    (("last name", "surname", "family name"), "last_name"),
    (("full name", "legal name", "applicant name", "your name", "name"), "full_name"),
    (("email address", "e mail", "email"), "email"),
    (
        (
            "whatsapp",
            "mobile number",
            "phone number",
            "contact number",
            "telephone",
            "mobile",
            "phone",
        ),
        "phone",
    ),
    (
        ("home address", "mailing address", "street address", "postal address", "address"),
        "home_address",
    ),
    (("date of birth", "birth date", "birthday", "dob"), "date_of_birth"),
    (("national id", "cnic", "national identity", "id number", "identity number"), "national_id"),
    (("passport number", "passport no"), "passport_number"),
    (("country of citizenship", "citizenship", "nationality"), "nationality"),
    (("country of residence", "current country", "residence", "country"), "country_of_residence"),
    (("university", "college", "institution", "school name", "current school"), "institution"),
    (
        (
            "field of study",
            "area of study",
            "major",
            "discipline",
            "degree program",
            "programme of study",
        ),
        "major",
    ),
    (
        ("level of study", "degree level", "current degree", "study level", "academic level"),
        "degree_level",
    ),
    (("cgpa", "gpa", "grade point average", "grade point"), "gpa"),
    (
        ("expected graduation", "graduation year", "year of graduation", "graduation date"),
        "graduation_year",
    ),
    (("technical skills", "skills"), "skills"),
    (
        ("languages you speak", "spoken languages", "language proficiency", "languages spoken"),
        "languages",
    ),
]

DOC_KEYWORDS: list[tuple[tuple[str, ...], DocKind]] = [
    (("cv", "resume", "curriculum vitae", "résumé"), DocKind.CV),
    (("transcript", "academic record", "marksheet", "grade report"), DocKind.TRANSCRIPT),
    (("passport", "national id", "identity", "id card", "identification"), DocKind.ID),
    (("certificate", "diploma", "degree certificate"), DocKind.CERTIFICATE),
]

ESSAY_WORDS = (
    "essay",
    "statement",
    "motivation",
    "why do you",
    "why are you",
    "why should",
    "describe",
    "tell us",
    "explain",
    "discuss",
    "what will you",
    "how will",
    "cover letter",
    "personal",
)

DEGREE_OPTION_WORDS = {
    "HIGH_SCHOOL": ("high school", "secondary", "a level", "intermediate"),
    "BACHELOR": ("bachelor", "undergraduate", "undergrad", "bs", "ba", "bsc"),
    "MASTER": ("master", "graduate", "postgraduate", "ms", "msc", "ma"),
    "PHD": ("phd", "doctor", "doctoral"),
}

_NON_WORD = re.compile(r"[^a-z0-9 ]+")


def normalize_label(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def _contains(haystack: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", haystack) is not None


def match_profile_key(label: str) -> str | None:
    norm = normalize_label(label)
    best: tuple[int, str] | None = None
    for phrases, key in SYNONYMS:
        for phrase in phrases:
            if _contains(norm, phrase) and (best is None or len(phrase) > best[0]):
                best = (len(phrase), key)
    return best[1] if best else None


def match_option(value: str, options: list[str], fact_key: str | None = None) -> str | None:
    if not options:
        return value
    v = normalize_label(value)
    if fact_key == "degree_level" and value.upper() in DEGREE_OPTION_WORDS:
        for option in options:
            o = normalize_label(option)
            if any(_contains(o, w) for w in DEGREE_OPTION_WORDS[value.upper()]):
                return option
    for option in options:
        if normalize_label(option) == v:
            return option
    for option in options:
        o = normalize_label(option)
        if o and (_contains(o, v) or _contains(v, o)):
            return option
    return None


def is_essay_field(f: FormField) -> bool:
    if f.field_type != "textarea":
        return False
    label = normalize_label(f.label)
    long_enough = f.max_length is None or f.max_length >= 400
    return long_enough and any(_contains(label, w) for w in ESSAY_WORDS)


@dataclass(frozen=True, slots=True)
class DocumentRef:
    document_id: uuid.UUID
    kind: DocKind
    filename: str


Recall = Callable[[str], Awaitable[RecallResult]]
EssayWriter = Callable[[FormField], Awaitable[EssayDraft | None]]


@dataclass
class FieldMapper:
    facts: dict[str, str]
    documents: list[DocumentRef]
    recall: Recall
    essay: EssayWriter
    known: dict[str, FieldResolution] = field(default_factory=dict)

    async def resolve(self, f: FormField) -> FieldResolution:
        # 0. settled values (the student's answers, or values from earlier runs) win.
        settled = self.known.get(f.key)
        if (
            settled is not None
            and settled.value is not None
            and settled.confidence >= AUTO_FILL_THRESHOLD
        ):
            return settled

        # 1. profile synonyms (skipped for essay prompts, file inputs)
        if f.field_type != "file" and not is_essay_field(f):
            key = match_profile_key(f.label) or match_profile_key(f.key.replace("_", " "))
            if key and self.facts.get(key):
                value = match_option(self.facts[key], f.options, key)
                if value is not None:
                    return FieldResolution(
                        f.key, value, FieldSource.PROFILE, PROFILE_CONFIDENCE, note=key
                    )

        # 2. document slots
        if f.field_type == "file":
            return self._document(f)

        # 3. essay prompts
        if is_essay_field(f):
            draft = await self.essay(f)
            if draft is None:
                return FieldResolution(
                    f.key, None, FieldSource.NONE, 0.0, note="essay engine unavailable"
                )
            if draft.requires_manual:
                return FieldResolution(
                    f.key,
                    None,
                    FieldSource.ESSAY,
                    0.0,
                    essay_draft_id=draft.id,
                    suggestion=draft.body,
                    note="this opportunity prohibits AI-written essays: author it yourself",
                )
            body = draft.body[: f.max_length] if f.max_length else draft.body
            return FieldResolution(
                f.key, body, FieldSource.ESSAY, 0.9, essay_draft_id=draft.id, note="stage 2 draft"
            )

        # 4. semantic memory
        result = await self.recall(f.label)
        best = result.best
        if best is not None:
            similarity = best.confidence.value
            answer = best.qa.answer_text
            value = match_option(answer, f.options)
            if best.confidence.auto_fill and value is not None:
                return FieldResolution(
                    f.key,
                    value,
                    FieldSource.MEMORY,
                    similarity,
                    memory_qa_id=best.qa.id,
                    note=f"recalled: {best.qa.question_text[:80]}",
                )
            return FieldResolution(
                f.key,
                None,
                FieldSource.NONE,
                similarity,
                memory_qa_id=best.qa.id,
                suggestion=answer,
                note="memory match below threshold",
            )
        return FieldResolution(f.key, None, FieldSource.NONE, 0.0, note="no source can answer this")

    def _document(self, f: FormField) -> FieldResolution:
        label = normalize_label(f"{f.label} {f.key}")
        for words, kind in DOC_KEYWORDS:
            if any(_contains(label, w) for w in words):
                match = next((d for d in self.documents if d.kind is kind), None)
                if match is not None:
                    return FieldResolution(
                        f.key,
                        match.filename,
                        FieldSource.DOCUMENT,
                        0.95,
                        document_id=match.document_id,
                        note=kind.value,
                    )
                return FieldResolution(
                    f.key,
                    None,
                    FieldSource.NONE,
                    0.0,
                    note=f"upload a {kind.value} document to the vault",
                )
        return FieldResolution(f.key, None, FieldSource.NONE, 0.0, note="unrecognized upload slot")
