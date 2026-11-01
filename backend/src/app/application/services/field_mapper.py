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
from dataclasses import dataclass, field, replace

from app.domain.applications.filling import FieldResolution, FieldSource, FormField
from app.domain.essay.synthesis import EssayDraft
from app.domain.memory.models import AUTO_FILL_THRESHOLD, RecallResult
from app.domain.profile.evidence import EvidenceAnswer
from app.domain.shared.enums import DocKind

PROFILE_CONFIDENCE = 0.97
PROFILE_V2_CONFIDENCE = 0.95  # profile v2 facts (links, city, referees, tests...)

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

# Profile v2 (deterministic, PROFILE_V2_CONFIDENCE). Matched together with SYNONYMS: the longest
# matching phrase wins, so "emergency contact phone" beats "phone".
SYNONYMS_V2: list[tuple[tuple[str, ...], str]] = [
    (("linkedin profile", "linkedin url", "linkedin"), "linkedin_url"),
    (("github profile", "github url", "github username", "github"), "github_url"),
    (
        (
            "portfolio url",
            "portfolio",
            "personal website",
            "personal site",
            "website url",
            "website",
        ),
        "portfolio_url",
    ),
    (("orcid", "google scholar", "scholar profile", "researchgate"), "orcid_or_scholar_url"),
    (("city of residence", "current city", "home city", "city", "town"), "city"),
    (("state or province", "state province", "province", "state region"), "state_or_province"),
    (("postal code", "zip code", "post code", "postcode", "zip"), "postal_code"),
    (("gender", "sex"), "gender"),
    (("marital status", "marital"), "marital_status"),
    (
        (
            "current semester",
            "current year of study",
            "year of study",
            "semester",
            "current year",
            "academic year",
        ),
        "current_semester_or_year",
    ),
    (
        (
            "secondary school qualification",
            "high school qualification",
            "intermediate qualification",
            "secondary education",
            "high school",
            "secondary school",
        ),
        "secondary_school_qualification",
    ),
    (
        (
            "high school gpa",
            "high school percentage",
            "secondary school gpa",
            "secondary school percentage",
            "intermediate marks",
            "intermediate percentage",
            "a level grades",
            "hssc marks",
            "fsc marks",
        ),
        "secondary_school_percentage",
    ),
    (("cgpa percentage", "percentage of marks", "aggregate percentage"), "cgpa_percentage"),
    (("minor", "specialization", "specialisation", "concentration"), "minor_or_specialization"),
    (
        (
            "thesis title",
            "fyp title",
            "final year project",
            "dissertation title",
            "research topic",
            "thesis",
        ),
        "thesis_or_fyp_title",
    ),
    (
        (
            "english language proficiency",
            "english proficiency",
            "english language test",
            "english test",
            "ielts score",
            "toefl score",
            "ielts",
            "toefl",
            "duolingo",
        ),
        "english_proficiency",
    ),
    (("research interests", "research areas", "areas of interest"), "research_interests"),
    (("number of publications", "publications count"), "publications_count"),
    (("academic honors", "academic honours", "honors and awards", "honours"), "academic_honors"),
    (
        (
            "require visa sponsorship",
            "need visa sponsorship",
            "visa sponsorship",
            "require sponsorship",
            "need sponsorship",
        ),
        "requires_visa_sponsorship",
    ),
    (("valid passport", "hold a passport", "have a passport"), "has_valid_passport"),
    (
        ("preferred work mode", "work mode", "work arrangement", "remote or onsite"),
        "preferred_work_mode",
    ),
    (
        ("earliest start date", "available from", "availability date", "start date"),
        "earliest_start_date",
    ),
    (("first generation", "first gen"), "first_generation_student"),
    (("financial need", "need based"), "financial_need_eligible"),
    (("gre score", "gre"), "test_gre"),
    (("sat score", "sat"), "test_sat"),
    (("gat score", "gat"), "test_gat"),
    (("gmat score", "gmat"), "test_gmat"),
    (("emergency contact name", "emergency contact"), "emergency_contact_name"),
    (
        ("emergency contact phone", "emergency contact number", "emergency phone"),
        "emergency_contact_phone",
    ),
    (
        ("emergency contact relationship", "relationship to emergency"),
        "emergency_contact_relationship",
    ),
]
V2_KEYS = frozenset(key for _, key in SYNONYMS_V2) | frozenset(
    f"referee_{i}_{k}"
    for i in (1, 2, 3)
    for k in ("name", "title", "department", "organization", "email", "phone", "relationship")
)
_REFEREE = re.compile(
    r"\b(referee|recommender|reference|recommendation letter writer|recommendation)s?\b"
    r"(?:\s*(?:no\s*)?(1|2|3|one|two|three))?"
    r"|\b(first|second|third|1st|2nd|3rd)\s+(referee|recommender|reference)\b"
)
_ORDINALS = {
    "1": 1,
    "2": 2,
    "3": 3,
    "one": 1,
    "two": 2,
    "three": 3,
    "first": 1,
    "second": 2,
    "third": 3,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
}
_REFEREE_PARTS: list[tuple[tuple[str, ...], str]] = [
    (("email", "e mail"), "email"),
    (("phone", "mobile", "contact number", "telephone"), "phone"),
    (("job title", "position", "designation", "title"), "title"),
    (("department", "faculty"), "department"),
    (
        ("institution", "organization", "organisation", "university", "affiliation", "employer"),
        "organization",
    ),
    (("relationship", "how do you know", "capacity"), "relationship"),
    (("full name", "name"), "name"),
]


def match_referee_key(label: str) -> str | None:
    """ "Referee 2 Email" / "Second recommender's institution" -> referee_2_email."""
    norm = normalize_label(label)
    m = _REFEREE.search(norm)
    if not m:
        return None
    word = m.group(2) or m.group(3) or "1"
    index = _ORDINALS.get(word, 1)
    for phrases, part in _REFEREE_PARTS:
        if any(_contains(norm, p) for p in phrases):
            return f"referee_{index}_{part}"
    return None


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
    referee = match_referee_key(label)
    if referee is not None:
        return referee
    norm = normalize_label(label)
    if norm == "state" or (norm.startswith("state ") and len(norm.split()) <= 3):
        return "state_or_province"
    best: tuple[int, str] | None = None
    for phrases, key in SYNONYMS + SYNONYMS_V2:
        for phrase in phrases:
            if _contains(norm, phrase) and (best is None or len(phrase) > best[0]):
                best = (len(phrase), key)
    if (
        best
        and best[1] == "secondary_school_qualification"
        and re.search(r"\b(gpa|cgpa|percentage|percent|marks|grades?|score|result)\b", norm)
    ):
        return "secondary_school_percentage"
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
# (field, close past answers as (question, answer, similarity)) -> grounded evidence answer
EvidenceLookup = Callable[[FormField, list[tuple[str, str, float]]], Awaitable[EvidenceAnswer]]
_EVIDENCE_SOURCE = {
    "DOCUMENT": FieldSource.DOCUMENT,
    "ESSAY": FieldSource.ESSAY,
    "MEMORY": FieldSource.MEMORY,
}


@dataclass
class FieldMapper:
    facts: dict[str, str]
    documents: list[DocumentRef]
    recall: Recall
    essay: EssayWriter
    known: dict[str, FieldResolution] = field(default_factory=dict)
    evidence: EvidenceLookup | None = None  # unified CV / transcript / essay / memory RAG
    # Tier 1 (Teach Mode): normalized label / field name -> the profile fact the student used.
    learned: dict[str, str] = field(default_factory=dict)

    async def resolve(self, f: FormField) -> FieldResolution:
        # 0. settled values (the student's answers, or values from earlier runs) win.
        settled = self.known.get(f.key)
        if (
            settled is not None
            and settled.value is not None
            and settled.confidence >= AUTO_FILL_THRESHOLD
        ):
            return settled

        # 0b. what the student taught on this portal (labels no synonym list knows)
        taught = self.learned.get(normalize_label(f.label)) or self.learned.get(f.key.lower())
        if taught and f.field_type != "file" and self.facts.get(taught):
            value = match_option(self.facts[taught], f.options, taught)
            if value is not None:
                return FieldResolution(
                    f.key, value, FieldSource.PROFILE, PROFILE_CONFIDENCE, note=f"taught:{taught}"
                )

        # 1. profile synonyms (skipped for essay prompts, file inputs)
        if f.field_type != "file" and not is_essay_field(f):
            key = match_profile_key(f.label) or match_profile_key(f.key.replace("_", " "))
            if key and self.facts.get(key):
                value = match_option(self.facts[key], f.options, key)
                if value is not None:
                    confidence = PROFILE_V2_CONFIDENCE if key in V2_KEYS else PROFILE_CONFIDENCE
                    return FieldResolution(f.key, value, FieldSource.PROFILE, confidence, note=key)

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
        miss = (
            FieldResolution(
                f.key,
                None,
                FieldSource.NONE,
                best.confidence.value,
                memory_qa_id=best.qa.id,
                suggestion=best.qa.answer_text,
                note="memory match below threshold",
            )
            if best is not None
            else FieldResolution(
                f.key, None, FieldSource.NONE, 0.0, note="no source can answer this"
            )
        )

        # 5. personal evidence RAG (CV / transcript / essays / past answers), required fields only:
        #    this is the last stop before the question pauses the application.
        if self.evidence is None or not f.required:
            return miss
        memory = [
            (m.qa.question_text, m.qa.answer_text, m.confidence.value) for m in result.matches[:3]
        ]
        found = await self.evidence(f, memory)
        if found.answer is None:
            return miss
        value = match_option(found.answer, f.options)
        if found.usable and value is not None:
            if f.max_length:
                value = value[: f.max_length]
            return FieldResolution(
                f.key,
                value,
                _EVIDENCE_SOURCE.get(found.source or "", FieldSource.DOCUMENT),
                found.confidence,
                note=f"evidence ({found.reason})",
                evidence=found.snippet,
            )
        return replace(
            miss,
            confidence=max(miss.confidence, found.confidence),
            suggestion=found.answer,
            evidence=found.snippet,
            note=f"evidence below threshold ({found.reason})",
        )

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
