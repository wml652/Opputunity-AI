from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.application.essay.use_cases import FingerprintView
from app.application.profile.use_cases import ProfileView
from app.domain.essay.models import (
    MAX_REFERENCE_ESSAYS,
    RECOMMENDED_ESSAYS,
    ReferenceEssay,
    StyleFingerprint,
)
from app.domain.memory.models import KnowledgeQA, MemorySource, RecallResult
from app.domain.profile.documents import Document
from app.domain.profile.models import (
    MAX_REFEREES,
    REFEREE_KEYS,
    DegreeLevel,
    EnglishProficiency,
    WorkMode,
)
from app.domain.shared.enums import DocKind

# ---- profile ------------------------------------------------------------------------------------


class EmergencyContactIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    relationship: str | None = Field(default=None, max_length=60)


class RefereeIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=200)
    organization: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    relationship: str | None = Field(default=None, max_length=120)


class ProfileDetailsIn(BaseModel):
    current_semester_or_year: str | None = Field(default=None, max_length=60)
    cgpa_percentage: float | None = Field(default=None, ge=0, le=100)
    secondary_school_qualification: str | None = Field(default=None, max_length=200)
    secondary_school_percentage: float | None = Field(default=None, ge=0, le=100)
    minor_or_specialization: str | None = Field(default=None, max_length=200)
    thesis_or_fyp_title: str | None = Field(default=None, max_length=300)
    academic_honors: list[str] = Field(default_factory=list, max_length=30)
    english_proficiency_type: EnglishProficiency | None = None
    english_proficiency_score: str | None = Field(default=None, max_length=20)
    standardized_tests: dict[str, str] = Field(default_factory=dict, max_length=12)
    publications_count: int | None = Field(default=None, ge=0, le=1000)
    research_interests: list[str] = Field(default_factory=list, max_length=30)
    preferred_work_mode: WorkMode | None = None
    earliest_start_date: str | None = Field(default=None, max_length=10)
    requires_visa_sponsorship: bool | None = None
    has_valid_passport: bool | None = None
    financial_need_eligible: bool | None = None
    first_generation_student: bool | None = None


class PIIIn(BaseModel):
    home_address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=50)
    date_of_birth: str | None = Field(default=None, max_length=20)
    national_id: str | None = Field(default=None, max_length=100)
    passport_number: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=120)
    state_or_province: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=20)
    gender: str | None = Field(default=None, max_length=40)
    marital_status: str | None = Field(default=None, max_length=40)
    linkedin_url: str | None = Field(default=None, max_length=300)
    github_url: str | None = Field(default=None, max_length=300)
    portfolio_url: str | None = Field(default=None, max_length=300)
    orcid_or_scholar_url: str | None = Field(default=None, max_length=300)
    emergency_contact: EmergencyContactIn | None = None
    referees: list[RefereeIn] = Field(default_factory=list, max_length=MAX_REFEREES)

    def flat(self) -> dict[str, str | None]:
        """The encrypted PII document is flat: referee_1_email, emergency_contact_phone..."""
        data: dict[str, str | None] = self.model_dump(exclude={"emergency_contact", "referees"})
        if self.emergency_contact is not None:
            for k, v in self.emergency_contact.model_dump().items():
                data[f"emergency_contact_{k}"] = v
        for i, ref in enumerate(self.referees[:MAX_REFEREES], start=1):
            for k, v in ref.model_dump().items():
                data[f"referee_{i}_{k}"] = v
        return data

    @staticmethod
    def of(flat: dict[str, str | None]) -> PIIIn:
        base = {k: v for k, v in flat.items() if not k.startswith(("referee_", "emergency_"))}
        emergency = {
            k: flat.get(f"emergency_contact_{k}") for k in ("name", "phone", "relationship")
        }
        referees = []
        for i in range(1, MAX_REFEREES + 1):
            ref = {k: flat.get(f"referee_{i}_{k}") for k in REFEREE_KEYS}
            if any(ref.values()):
                referees.append(RefereeIn(**ref))
        return PIIIn(
            **base,
            emergency_contact=EmergencyContactIn(**emergency) if any(emergency.values()) else None,
            referees=referees,
        )


class LanguageIn(BaseModel):
    name: str = Field(max_length=60)
    level: str = Field(default="", max_length=40)


class ExperienceIn(BaseModel):
    title: str = Field(max_length=200)
    organization: str = Field(default="", max_length=200)
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    description: str = Field(default="", max_length=2000)


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    pii: PIIIn = Field(default_factory=PIIIn)
    degree_level: DegreeLevel | None = None
    major: str | None = Field(default=None, max_length=200)
    institution: str | None = Field(default=None, max_length=200)
    gpa: float | None = Field(default=None, ge=0, le=100)
    gpa_scale: float | None = Field(default=None, gt=0, le=100)
    nationality: str | None = Field(default=None, max_length=100)
    country_of_residence: str | None = Field(default=None, max_length=100)
    graduation_year: int | None = Field(default=None, ge=1950, le=2100)
    skills: list[str] = Field(default_factory=list, max_length=100)
    languages: list[LanguageIn] = Field(default_factory=list, max_length=30)
    experiences: list[ExperienceIn] = Field(default_factory=list, max_length=50)
    details: ProfileDetailsIn = Field(default_factory=ProfileDetailsIn)


class ProfileOut(ProfileIn):
    id: uuid.UUID
    gpa_normalized_4: float | None
    pii_encrypted: bool
    pii_key_version: int | None
    updated_at: datetime | None

    @staticmethod
    def of(view: ProfileView) -> ProfileOut:
        p = view.profile
        return ProfileOut(
            id=p.id,
            full_name=p.full_name,
            pii=PIIIn.of(p.pii.to_dict()),
            details=ProfileDetailsIn(**p.details.to_dict()),
            degree_level=p.degree_level,
            major=p.major,
            institution=p.institution,
            gpa=float(p.gpa.value) if p.gpa else None,
            gpa_scale=float(p.gpa.scale) if p.gpa else None,
            gpa_normalized_4=float(p.gpa.normalized_4) if p.gpa else None,
            nationality=p.nationality,
            country_of_residence=p.country_of_residence,
            graduation_year=p.graduation_year,
            skills=p.skills,
            languages=[LanguageIn(name=lang.name, level=lang.level) for lang in p.languages],
            experiences=[
                ExperienceIn(
                    title=e.title,
                    organization=e.organization,
                    start=e.start,
                    end=e.end,
                    description=e.description,
                )
                for e in p.experiences
            ],
            pii_encrypted=view.pii_encrypted,
            pii_key_version=view.pii_key_version,
            updated_at=p.updated_at,
        )


# ---- documents ----------------------------------------------------------------------------------


class DocumentOut(BaseModel):
    id: uuid.UUID
    kind: DocKind
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    page_count: int | None
    text_chars: int
    created_at: datetime

    @staticmethod
    def of(d: Document) -> DocumentOut:
        return DocumentOut(
            id=d.id,
            kind=d.kind,
            filename=d.filename,
            mime=d.mime,
            size_bytes=d.size_bytes,
            sha256=d.sha256,
            page_count=d.page_count,
            text_chars=d.text_chars,
            created_at=d.created_at,
        )


class DocumentTextOut(BaseModel):
    document: DocumentOut
    text: str
    truncated: bool


# ---- essays -------------------------------------------------------------------------------------


class EssayIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=30_000)
    prompt_context: str | None = Field(default=None, max_length=1000)


class EssayOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    prompt_context: str | None
    word_count: int
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def of(e: ReferenceEssay) -> EssayOut:
        return EssayOut(
            id=e.id,
            title=e.title,
            body=e.body,
            prompt_context=e.prompt_context,
            word_count=e.word_count,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


class FingerprintOut(BaseModel):
    version: int
    essay_count: int
    total_words: int
    metrics: dict[str, Any]
    feature_names: list[str]
    feature_vector: list[float]
    created_at: datetime | None

    @staticmethod
    def of(f: StyleFingerprint) -> FingerprintOut:
        return FingerprintOut(
            version=f.version,
            essay_count=f.essay_count,
            total_words=f.total_words,
            metrics=f.metrics,
            feature_names=f.feature_names,
            feature_vector=f.feature_vector,
            created_at=f.created_at,
        )


class FingerprintStatusOut(BaseModel):
    fingerprint: FingerprintOut | None
    essay_count: int
    chunk_count: int
    max_essays: int = MAX_REFERENCE_ESSAYS
    recommended_essays: int = RECOMMENDED_ESSAYS

    @staticmethod
    def of(view: FingerprintView) -> FingerprintStatusOut:
        return FingerprintStatusOut(
            fingerprint=FingerprintOut.of(view.fingerprint) if view.fingerprint else None,
            essay_count=view.essay_count,
            chunk_count=view.chunk_count,
        )


class EssayMutationOut(BaseModel):
    essay: EssayOut | None
    fingerprint: FingerprintOut | None


# ---- memory -------------------------------------------------------------------------------------


class MemoryIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=10_000)


class MemoryPatch(BaseModel):
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    answer: str | None = Field(default=None, min_length=1, max_length=10_000)


class MemoryOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str
    source: MemorySource
    embedding_model: str
    times_reused: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def of(qa: KnowledgeQA) -> MemoryOut:
        return MemoryOut(
            id=qa.id,
            question=qa.question_text,
            answer=qa.answer_text,
            source=qa.source,
            embedding_model=qa.embedding_model,
            times_reused=qa.times_reused,
            last_used_at=qa.last_used_at,
            created_at=qa.created_at,
            updated_at=qa.updated_at,
        )


class MemoryPageOut(BaseModel):
    items: list[MemoryOut]
    total: int


class RecallIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)


class RecallMatchOut(BaseModel):
    memory: MemoryOut
    similarity: float
    auto_fill: bool


class RecallOut(BaseModel):
    query: str
    embedding_model: str
    threshold: float
    auto_fill: bool
    best: RecallMatchOut | None
    matches: list[RecallMatchOut]
    searched: int
    stale: int

    @staticmethod
    def of(result: RecallResult) -> RecallOut:
        matches = [
            RecallMatchOut(
                memory=MemoryOut.of(m.qa),
                similarity=round(m.confidence.value, 4),
                auto_fill=m.confidence.auto_fill,
            )
            for m in result.matches
        ]
        return RecallOut(
            query=result.query,
            embedding_model=result.embedding_model,
            threshold=result.threshold,
            auto_fill=result.auto_fill,
            best=matches[0] if matches else None,
            matches=matches,
            searched=result.searched,
            stale=result.stale,
        )
