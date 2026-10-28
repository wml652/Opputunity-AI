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

    def to_dict(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in PII_FIELDS}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StudentPII:
        return StudentPII(**{k: (str(data[k]) if data.get(k) else None) for k in PII_FIELDS})

    def is_empty(self) -> bool:
        return not any(self.to_dict().values())


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
