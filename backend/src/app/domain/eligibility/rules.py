"""Eligibility reasoning, Stage 1: deterministic evaluators over structured criteria.

Each criterion yields PASS / FAIL / UNKNOWN with human-readable evidence. Only the free-text
`other_criteria` (and anything Stage 1 cannot decide) is left for the Stage 2 LLM evaluator.
Outcome: deadline passed -> EXPIRED; any FAIL -> INELIGIBLE; any UNKNOWN -> UNCERTAIN;
otherwise ELIGIBLE.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.profile.models import DegreeLevel, StudentProfile
from app.domain.shared.enums import EligibilityOutcome


class RuleStatus(enum.StrEnum):
    PASS = "PASS"  # noqa: S105 - an eligibility verdict, not a password
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class RuleSource(enum.StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    LLM = "LLM"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: str
    status: RuleStatus
    evidence: str
    source: RuleSource = RuleSource.DETERMINISTIC

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "status": self.status.value,
            "evidence": self.evidence,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class EligibilityRules:
    """Structured criteria extracted from a listing. Empty list / None = no restriction stated."""

    degree_levels: list[DegreeLevel] = field(default_factory=list)
    min_gpa: Decimal | None = None
    gpa_scale: Decimal | None = None
    nationalities: list[str] = field(default_factory=list)
    residency_countries: list[str] = field(default_factory=list)
    graduation_year_min: int | None = None
    graduation_year_max: int | None = None
    other_criteria: list[str] = field(default_factory=list)
    # Profile v2 criteria. None / empty = not stated.
    english_tests: dict[str, float] = field(default_factory=dict)  # {"IELTS": 6.5}
    moi_accepted: bool | None = None  # an English-medium degree waives the test
    visa_sponsorship: bool | None = None  # False = "no visa sponsorship"
    requires_passport: bool | None = None
    financial_need_required: bool | None = None
    research_areas: list[str] = field(default_factory=list)  # ranking signal
    work_mode: str | None = None  # REMOTE | HYBRID | ONSITE (ranking signal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "degree_levels": [d.value for d in self.degree_levels],
            "min_gpa": str(self.min_gpa) if self.min_gpa is not None else None,
            "gpa_scale": str(self.gpa_scale) if self.gpa_scale is not None else None,
            "nationalities": self.nationalities,
            "residency_countries": self.residency_countries,
            "graduation_year_min": self.graduation_year_min,
            "graduation_year_max": self.graduation_year_max,
            "other_criteria": self.other_criteria,
            "english_tests": self.english_tests,
            "moi_accepted": self.moi_accepted,
            "visa_sponsorship": self.visa_sponsorship,
            "requires_passport": self.requires_passport,
            "financial_need_required": self.financial_need_required,
            "research_areas": self.research_areas,
            "work_mode": self.work_mode,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EligibilityRules:
        def dec(v: Any) -> Decimal | None:
            if v in (None, ""):
                return None
            try:
                return Decimal(str(v))
            except InvalidOperation:
                return None

        levels = []
        for raw in data.get("degree_levels") or []:
            try:
                levels.append(DegreeLevel(str(raw).upper()))
            except ValueError:
                continue
        return EligibilityRules(
            degree_levels=levels,
            min_gpa=dec(data.get("min_gpa")),
            gpa_scale=dec(data.get("gpa_scale")),
            nationalities=[str(x) for x in data.get("nationalities") or [] if str(x).strip()],
            residency_countries=[
                str(x) for x in data.get("residency_countries") or [] if str(x).strip()
            ],
            graduation_year_min=_int(data.get("graduation_year_min")),
            graduation_year_max=_int(data.get("graduation_year_max")),
            other_criteria=[str(x) for x in data.get("other_criteria") or [] if str(x).strip()],
            english_tests=_english_tests(data.get("english_tests")),
            moi_accepted=_bool(data.get("moi_accepted")),
            visa_sponsorship=_bool(data.get("visa_sponsorship")),
            requires_passport=_bool(data.get("requires_passport")),
            financial_need_required=_bool(data.get("financial_need_required")),
            research_areas=[
                str(x)[:120] for x in data.get("research_areas") or [] if str(x).strip()
            ][:20],
            work_mode=(
                str(data["work_mode"]).upper()
                if str(data.get("work_mode") or "").upper() in ("REMOTE", "HYBRID", "ONSITE")
                else None
            ),
        )


ENGLISH_TESTS = ("IELTS", "TOEFL", "DUOLINGO", "PTE")


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _english_tests(value: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(value, dict):
        return out
    for k, v in value.items():
        test = str(k).strip().upper()
        try:
            score = float(v)
        except (TypeError, ValueError):
            continue
        if test in ENGLISH_TESTS and 0 < score <= 200:
            out[test] = score
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---- country matching ---------------------------------------------------------------------------

_OPEN_TO_ALL = {"any", "all", "international", "worldwide", "global", "all countries", "open"}
_ALIASES = {
    "us": "united states",
    "usa": "united states",
    "u.s.": "united states",
    "united states of america": "united states",
    "america": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "britain": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "uae": "united arab emirates",
    "ksa": "saudi arabia",
    "pk": "pakistan",
    "pakistani": "pakistan",
    "indian": "india",
    "nigerian": "nigeria",
    "kenyan": "kenya",
    "bangladeshi": "bangladesh",
    "egyptian": "egypt",
    "german": "germany",
    "american": "united states",
    "british": "united kingdom",
    "canadian": "canada",
    "chinese": "china",
}


def canonical_country(value: str) -> str:
    key = " ".join(value.strip().lower().split())
    return _ALIASES.get(key, key)


def _country_rule(name: str, allowed: list[str], student_value: str | None) -> RuleResult:
    if not allowed or any(canonical_country(a) in _OPEN_TO_ALL for a in allowed):
        return RuleResult(name, RuleStatus.PASS, "no restriction stated")
    if not student_value:
        return RuleResult(
            name,
            RuleStatus.UNKNOWN,
            f"profile has no {name}; listing requires {', '.join(allowed)}",
        )
    wanted = {canonical_country(a) for a in allowed}
    if canonical_country(student_value) in wanted:
        return RuleResult(name, RuleStatus.PASS, f"{student_value} is in {', '.join(allowed)}")
    return RuleResult(name, RuleStatus.FAIL, f"{student_value} is not in {', '.join(allowed)}")


# ---- evaluators ---------------------------------------------------------------------------------


def evaluate_deadline(deadline_at: datetime | None, now: datetime) -> RuleResult:
    if deadline_at is None:
        return RuleResult("deadline", RuleStatus.UNKNOWN, "no deadline found on the listing")
    if deadline_at <= now:
        return RuleResult("deadline", RuleStatus.FAIL, f"deadline {deadline_at.date()} has passed")
    days = (deadline_at - now).days
    return RuleResult(
        "deadline", RuleStatus.PASS, f"open until {deadline_at.date()} ({days} days left)"
    )


def evaluate_degree(rules: EligibilityRules, profile: StudentProfile) -> RuleResult:
    if not rules.degree_levels:
        return RuleResult("degree_level", RuleStatus.PASS, "no restriction stated")
    wanted = ", ".join(d.value for d in rules.degree_levels)
    if profile.degree_level is None:
        return RuleResult(
            "degree_level", RuleStatus.UNKNOWN, f"profile has no degree level; requires {wanted}"
        )
    if profile.degree_level in rules.degree_levels:
        return RuleResult(
            "degree_level", RuleStatus.PASS, f"{profile.degree_level.value} matches {wanted}"
        )
    return RuleResult(
        "degree_level", RuleStatus.FAIL, f"{profile.degree_level.value} is not one of {wanted}"
    )


def evaluate_gpa(rules: EligibilityRules, profile: StudentProfile) -> RuleResult:
    if rules.min_gpa is None:
        return RuleResult("gpa", RuleStatus.PASS, "no minimum GPA stated")
    scale = rules.gpa_scale if rules.gpa_scale and rules.gpa_scale > 0 else Decimal(4)
    if rules.min_gpa > scale:
        return RuleResult("gpa", RuleStatus.UNKNOWN, f"unclear requirement {rules.min_gpa}/{scale}")
    required_4 = (rules.min_gpa / scale * 4).quantize(Decimal("0.01"))
    if profile.gpa is None:
        return RuleResult(
            "gpa", RuleStatus.UNKNOWN, f"profile has no GPA; requires {rules.min_gpa}/{scale}"
        )
    student_4 = profile.gpa.normalized_4
    detail = (
        f"{profile.gpa.value}/{profile.gpa.scale} = {student_4}/4.00 vs required "
        f"{rules.min_gpa}/{scale} = {required_4}/4.00"
    )
    return RuleResult(
        "gpa", RuleStatus.PASS if student_4 >= required_4 else RuleStatus.FAIL, detail
    )


def evaluate_graduation(rules: EligibilityRules, profile: StudentProfile) -> RuleResult:
    lo, hi = rules.graduation_year_min, rules.graduation_year_max
    if lo is None and hi is None:
        return RuleResult("graduation_year", RuleStatus.PASS, "no window stated")
    window = f"{lo or '...'}-{hi or '...'}"
    if profile.graduation_year is None:
        return RuleResult(
            "graduation_year",
            RuleStatus.UNKNOWN,
            f"profile has no graduation year; window {window}",
        )
    year = profile.graduation_year
    ok = (lo is None or year >= lo) and (hi is None or year <= hi)
    return RuleResult(
        "graduation_year",
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        f"graduating {year}, window {window}",
    )


def evaluate_english(rules: EligibilityRules, profile: StudentProfile) -> RuleResult:
    """IELTS / TOEFL / Duolingo / PTE minimums; native speakers pass; an English-medium degree
    (MOI) passes only where the listing accepts MOI."""
    if not rules.english_tests and rules.moi_accepted is None:
        return RuleResult("english", RuleStatus.PASS, "no English requirement stated")
    wanted = ", ".join(f"{t} {s:g}" for t, s in rules.english_tests.items()) or "English proof"
    d = profile.details
    kind = d.english_proficiency_type.value if d.english_proficiency_type else None
    if kind == "NATIVE":
        return RuleResult("english", RuleStatus.PASS, f"native speaker; required {wanted}")
    if kind == "MOI_EXEMPT":
        if rules.moi_accepted:
            return RuleResult("english", RuleStatus.PASS, "English-medium degree (MOI) accepted")
        if rules.moi_accepted is False:
            return RuleResult("english", RuleStatus.FAIL, f"MOI not accepted; requires {wanted}")
        return RuleResult("english", RuleStatus.UNKNOWN, f"MOI not mentioned; requires {wanted}")
    if kind in rules.english_tests:
        try:
            score = float(str(d.english_proficiency_score or "").strip())
        except ValueError:
            return RuleResult("english", RuleStatus.UNKNOWN, f"{kind} score missing; {wanted}")
        need = rules.english_tests[kind]
        return RuleResult(
            "english",
            RuleStatus.PASS if score >= need else RuleStatus.FAIL,
            f"{kind} {score:g} vs required {need:g}",
        )
    if kind in (None, "NONE"):
        return RuleResult("english", RuleStatus.UNKNOWN, f"no English test in profile; {wanted}")
    return RuleResult("english", RuleStatus.UNKNOWN, f"profile has {kind}; listing asks {wanted}")


def _flag_rule(
    rule: str, required: bool | None, student: bool | None, need: str, fail: str
) -> RuleResult:
    if not required:
        return RuleResult(rule, RuleStatus.PASS, "not required")
    if student is None:
        return RuleResult(rule, RuleStatus.UNKNOWN, f"{need} - profile does not say")
    return RuleResult(
        rule, RuleStatus.PASS if student else RuleStatus.FAIL, need if student else fail
    )


def evaluate_visa(rules: EligibilityRules, profile: StudentProfile) -> RuleResult:
    needs = profile.details.requires_visa_sponsorship
    if rules.visa_sponsorship is not False:
        return RuleResult("visa", RuleStatus.PASS, "sponsorship not excluded")
    if needs is None:
        return RuleResult("visa", RuleStatus.UNKNOWN, "no visa sponsorship; profile does not say")
    if needs:
        return RuleResult("visa", RuleStatus.FAIL, "no visa sponsorship, student needs one")
    return RuleResult("visa", RuleStatus.PASS, "no sponsorship needed")


def evaluate_deterministic(
    rules: EligibilityRules,
    profile: StudentProfile,
    deadline_at: datetime | None,
    now: datetime,
) -> list[RuleResult]:
    d = profile.details
    return [
        evaluate_deadline(deadline_at, now),
        evaluate_degree(rules, profile),
        evaluate_gpa(rules, profile),
        _country_rule("nationality", rules.nationalities, profile.nationality),
        _country_rule("residency", rules.residency_countries, profile.country_of_residence),
        evaluate_graduation(rules, profile),
        evaluate_english(rules, profile),
        evaluate_visa(rules, profile),
        _flag_rule(
            "passport",
            rules.requires_passport,
            d.has_valid_passport,
            "valid passport",
            "a valid passport is required",
        ),
        _flag_rule(
            "financial_need",
            rules.financial_need_required,
            d.financial_need_eligible,
            "demonstrated financial need",
            "for students with financial need only",
        ),
    ]


def decide(results: list[RuleResult]) -> EligibilityOutcome:
    deadline = next((r for r in results if r.rule == "deadline"), None)
    if deadline is not None and deadline.status is RuleStatus.FAIL:
        return EligibilityOutcome.EXPIRED
    if any(r.status is RuleStatus.FAIL for r in results):
        return EligibilityOutcome.INELIGIBLE
    # A missing deadline alone does not block eligibility; other unknowns do.
    if any(r.status is RuleStatus.UNKNOWN and r.rule != "deadline" for r in results):
        return EligibilityOutcome.UNCERTAIN
    return EligibilityOutcome.ELIGIBLE


def profile_snapshot(profile: StudentProfile) -> dict[str, Any]:
    """Non-PII facts the Stage 2 LLM may see (never address/phone/IDs)."""
    return {
        "degree_level": profile.degree_level.value if profile.degree_level else None,
        "major": profile.major,
        "institution": profile.institution,
        "gpa": f"{profile.gpa.value}/{profile.gpa.scale}" if profile.gpa else None,
        "nationality": profile.nationality,
        "country_of_residence": profile.country_of_residence,
        "graduation_year": profile.graduation_year,
        "skills": profile.skills[:30],
        "languages": [f"{lang.name} ({lang.level})" for lang in profile.languages],
        "experiences": [
            f"{e.title} at {e.organization} ({e.start or '?'} - {e.end or 'present'}): "
            f"{e.description[:200]}"
            for e in profile.experiences[:10]
        ],
        # Profile v2 (non-PII only: links, city and referees stay encrypted and out of prompts).
        **{k: v for k, v in profile.details.to_dict().items() if v not in (None, [], {}, "")},
    }
