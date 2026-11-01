"""Student Profile Vault: academic identity used for eligibility, plus encrypted personal PII."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.shared.errors import ValidationError

PII_FIELDS = ("home_address", "phone", "date_of_birth", "national_id", "passport_number")
REFEREE_KEYS = ("name", "title", "department", "organization", "email", "phone", "relationship")
MAX_REFEREES = 3
# Profile v2 personal data - encrypted inside the same PII document. Links (LinkedIn, GitHub...)
# identify the student, so they are PII too and never reach a model.
PII_V2_FIELDS = (
    "city",
    "state_or_province",
    "postal_code",
    "gender",
    "marital_status",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "orcid_or_scholar_url",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    *(f"referee_{i}_{k}" for i in range(1, MAX_REFEREES + 1) for k in REFEREE_KEYS),
)
ALL_PII_FIELDS = PII_FIELDS + PII_V2_FIELDS


class DegreeLevel(enum.StrEnum):
    HIGH_SCHOOL = "HIGH_SCHOOL"
    BACHELOR = "BACHELOR"
    MASTER = "MASTER"
    PHD = "PHD"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class GPA:
    """A grade on its native scale; `normalized_4` maps it linearly onto 0-4.0 for comparisons."""

    value: Decimal
    scale: Decimal

    def __post_init__(self) -> None:
        if self.scale <= 0 or self.scale > 100:
            raise ValidationError("GPA scale must be between 0 and 100")
        if self.value < 0 or self.value > self.scale:
            raise ValidationError(f"GPA {self.value} is outside its scale 0-{self.scale}")

    @staticmethod
    def of(value: float | str | Decimal, scale: float | str | Decimal) -> GPA:
        try:
            return GPA(Decimal(str(value)), Decimal(str(scale)))
        except ArithmeticError as exc:
            raise ValidationError("GPA must be numeric") from exc

    @property
    def normalized_4(self) -> Decimal:
        return (self.value / self.scale * 4).quantize(Decimal("0.01"))

    @property
    def percent(self) -> Decimal:
        return (self.value / self.scale * 100).quantize(Decimal("0.1"))


@dataclass(frozen=True, slots=True)
class StudentPII:
    """Encrypted at rest as one JSON document (AES-256-GCM, row-bound AAD)."""

    home_address: str | None = None
    phone: str | None = None
    date_of_birth: str | None = None  # ISO date
    national_id: str | None = None
    passport_number: str | None = None
    # Profile v2 keys (PII_V2_FIELDS): city, links, emergency contact, referee_{1..3}_{...}.
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str | None]:
        base: dict[str, str | None] = {name: getattr(self, name) for name in PII_FIELDS}
        return base | {name: self.extra.get(name) for name in PII_V2_FIELDS}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StudentPII:
        return StudentPII(
            **{k: (str(data[k]) if data.get(k) else None) for k in PII_FIELDS},
            extra={k: str(data[k]) for k in PII_V2_FIELDS if data.get(k)},
        )

    def is_empty(self) -> bool:
        return not any(self.to_dict().values())


class EnglishProficiency(enum.StrEnum):
    IELTS = "IELTS"
    TOEFL = "TOEFL"
    DUOLINGO = "DUOLINGO"
    PTE = "PTE"
    MOI_EXEMPT = "MOI_EXEMPT"  # medium of instruction was English
    NATIVE = "NATIVE"
    NONE = "NONE"


class WorkMode(enum.StrEnum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    ANY = "ANY"


def _opt_float(v: Any, lo: float, hi: float) -> float | None:
    try:
        f = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return f if f is not None and lo <= f <= hi else None


def _opt_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _opt_str(v: Any, limit: int = 200) -> str | None:
    text = str(v).strip()[:limit] if v not in (None, "") else ""
    return text or None


def _str_list(v: Any, limit: int = 30) -> list[str]:
    return [str(x).strip()[:200] for x in (v or []) if str(x).strip()][:limit]


@dataclass(frozen=True, slots=True)
class ProfileDetails:
    """Profile v2: academic depth, English / tests, research and work preferences (non-PII:
    used by eligibility, ranking and the form filler; may be shown to the user's model)."""

    current_semester_or_year: str | None = None  # "6th Semester / 3rd Year"
    cgpa_percentage: float | None = None  # 0-100
    secondary_school_qualification: str | None = None  # "FSc Pre-Engineering / A-Levels"
    secondary_school_percentage: float | None = None
    minor_or_specialization: str | None = None
    thesis_or_fyp_title: str | None = None
    academic_honors: list[str] = field(default_factory=list)
    english_proficiency_type: EnglishProficiency | None = None
    english_proficiency_score: str | None = None
    standardized_tests: dict[str, str] = field(default_factory=dict)  # {"GRE": "320"}
    publications_count: int | None = None
    research_interests: list[str] = field(default_factory=list)
    preferred_work_mode: WorkMode | None = None
    earliest_start_date: str | None = None  # ISO date
    requires_visa_sponsorship: bool | None = None
    has_valid_passport: bool | None = None
    financial_need_eligible: bool | None = None
    first_generation_student: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_semester_or_year": self.current_semester_or_year,
            "cgpa_percentage": self.cgpa_percentage,
            "secondary_school_qualification": self.secondary_school_qualification,
            "secondary_school_percentage": self.secondary_school_percentage,
            "minor_or_specialization": self.minor_or_specialization,
            "thesis_or_fyp_title": self.thesis_or_fyp_title,
            "academic_honors": list(self.academic_honors),
            "english_proficiency_type": (
                self.english_proficiency_type.value if self.english_proficiency_type else None
            ),
            "english_proficiency_score": self.english_proficiency_score,
            "standardized_tests": dict(self.standardized_tests),
            "publications_count": self.publications_count,
            "research_interests": list(self.research_interests),
            "preferred_work_mode": (
                self.preferred_work_mode.value if self.preferred_work_mode else None
            ),
            "earliest_start_date": self.earliest_start_date,
            "requires_visa_sponsorship": self.requires_visa_sponsorship,
            "has_valid_passport": self.has_valid_passport,
            "financial_need_eligible": self.financial_need_eligible,
            "first_generation_student": self.first_generation_student,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> ProfileDetails:
        d = data or {}

        def enum_of(cls: type[enum.StrEnum], v: Any) -> Any:
            try:
                return cls(str(v).upper()) if v not in (None, "") else None
            except ValueError:
                return None

        pubs = d.get("publications_count")
        tests = d.get("standardized_tests") or {}
        return ProfileDetails(
            current_semester_or_year=_opt_str(d.get("current_semester_or_year"), 60),
            cgpa_percentage=_opt_float(d.get("cgpa_percentage"), 0, 100),
            secondary_school_qualification=_opt_str(d.get("secondary_school_qualification")),
            secondary_school_percentage=_opt_float(d.get("secondary_school_percentage"), 0, 100),
            minor_or_specialization=_opt_str(d.get("minor_or_specialization")),
            thesis_or_fyp_title=_opt_str(d.get("thesis_or_fyp_title"), 300),
            academic_honors=_str_list(d.get("academic_honors")),
            english_proficiency_type=enum_of(EnglishProficiency, d.get("english_proficiency_type")),
            english_proficiency_score=_opt_str(d.get("english_proficiency_score"), 20),
            standardized_tests={
                str(k).strip().upper()[:20]: str(v).strip()[:20]
                for k, v in (tests.items() if isinstance(tests, dict) else [])
                if str(k).strip() and str(v).strip()
            },
            publications_count=int(pubs) if isinstance(pubs, int) and 0 <= pubs <= 1000 else None,
            research_interests=_str_list(d.get("research_interests")),
            preferred_work_mode=enum_of(WorkMode, d.get("preferred_work_mode")),
            earliest_start_date=_opt_str(d.get("earliest_start_date"), 10),
            requires_visa_sponsorship=_opt_bool(d.get("requires_visa_sponsorship")),
            has_valid_passport=_opt_bool(d.get("has_valid_passport")),
            financial_need_eligible=_opt_bool(d.get("financial_need_eligible")),
            first_generation_student=_opt_bool(d.get("first_generation_student")),
        )


@dataclass(frozen=True, slots=True)
class Experience:
    title: str
    organization: str
    start: str | None = None  # YYYY-MM
    end: str | None = None  # YYYY-MM or None = ongoing
    description: str = ""


@dataclass(frozen=True, slots=True)
class Language:
    name: str
    level: str  # e.g. Native, C1, Fluent


@dataclass(slots=True)
class StudentProfile:
    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    pii: StudentPII
    degree_level: DegreeLevel | None
    major: str | None
    institution: str | None
    gpa: GPA | None
    nationality: str | None
    country_of_residence: str | None
    graduation_year: int | None
    skills: list[str] = field(default_factory=list)
    languages: list[Language] = field(default_factory=list)
    experiences: list[Experience] = field(default_factory=list)
    details: ProfileDetails = field(default_factory=ProfileDetails)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def validate(self) -> None:
        if not self.full_name.strip():
            raise ValidationError("full_name is required")
        if self.graduation_year is not None and not (1950 <= self.graduation_year <= 2100):
            raise ValidationError("graduation_year looks invalid")
        if len(self.skills) > 100:
            raise ValidationError("too many skills (max 100)")
        if len(self.experiences) > 50:
            raise ValidationError("too many experiences (max 50)")


def clean_skills(raw: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in raw:
        skill = item.strip()[:80]
        if skill and skill.lower() not in seen:
            seen[skill.lower()] = skill
    return list(seen.values())
