"""Eligibility matrix: deterministic Stage 1 + outcome decision."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.eligibility.rules import (
    EligibilityRules,
    RuleResult,
    RuleSource,
    RuleStatus,
    canonical_country,
    decide,
    evaluate_deterministic,
    profile_snapshot,
)
from app.domain.profile.models import GPA, DegreeLevel, Experience, StudentPII, StudentProfile
from app.domain.shared.enums import EligibilityOutcome

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(days=60)
PAST = NOW - timedelta(days=1)


def profile(**overrides: object) -> StudentProfile:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "full_name": "Ayesha",
        "pii": StudentPII(phone="+92 300 1234567"),
        "degree_level": DegreeLevel.BACHELOR,
        "major": "CS",
        "institution": "LUMS",
        "gpa": GPA.of("3.62", "4"),
        "nationality": "Pakistan",
        "country_of_residence": "Pakistan",
        "graduation_year": 2027,
        "experiences": [Experience("Team lead", "Robotics club")],
    }
    base.update(overrides)
    return StudentProfile(**base)  # type: ignore[arg-type]


def outcome(
    rules: EligibilityRules, p: StudentProfile | None = None, deadline: datetime | None = FUTURE
) -> EligibilityOutcome:
    return decide(evaluate_deterministic(rules, p or profile(), deadline, NOW))


def test_open_listing_is_eligible() -> None:
    assert outcome(EligibilityRules()) is EligibilityOutcome.ELIGIBLE


def test_passed_deadline_is_expired_even_if_otherwise_ineligible() -> None:
    rules = EligibilityRules(degree_levels=[DegreeLevel.PHD])
    assert outcome(rules, deadline=PAST) is EligibilityOutcome.EXPIRED
    assert outcome(EligibilityRules(), deadline=NOW) is EligibilityOutcome.EXPIRED  # <= now


def test_missing_deadline_alone_does_not_block() -> None:
    results = evaluate_deterministic(EligibilityRules(), profile(), None, NOW)
    assert next(r for r in results if r.rule == "deadline").status is RuleStatus.UNKNOWN
    assert decide(results) is EligibilityOutcome.ELIGIBLE


def test_degree_mismatch_is_ineligible() -> None:
    assert outcome(EligibilityRules(degree_levels=[DegreeLevel.MASTER, DegreeLevel.PHD])) is (
        EligibilityOutcome.INELIGIBLE
    )
    assert (
        outcome(EligibilityRules(degree_levels=[DegreeLevel.BACHELOR]))
        is EligibilityOutcome.ELIGIBLE
    )


@pytest.mark.parametrize(
    ("min_gpa", "scale", "student", "student_scale", "expected"),
    [
        ("3.0", "4", "3.62", "4", EligibilityOutcome.ELIGIBLE),
        ("3.7", "4", "3.62", "4", EligibilityOutcome.INELIGIBLE),
        ("85", "100", "3.62", "4", EligibilityOutcome.ELIGIBLE),  # 3.40 vs 3.62
        ("95", "100", "3.62", "4", EligibilityOutcome.INELIGIBLE),  # 3.80 vs 3.62
        ("4.0", "5", "8.5", "10", EligibilityOutcome.ELIGIBLE),  # 3.20 vs 3.40
        ("3.5", "4", "80", "100", EligibilityOutcome.INELIGIBLE),  # 3.50 vs 3.20
        ("3.0", None, "3.1", "4", EligibilityOutcome.ELIGIBLE),  # scale defaults to 4
    ],
)
def test_gpa_scale_normalization(
    min_gpa: str, scale: str | None, student: str, student_scale: str, expected: EligibilityOutcome
) -> None:
    rules = EligibilityRules(min_gpa=Decimal(min_gpa), gpa_scale=Decimal(scale) if scale else None)
    assert outcome(rules, profile(gpa=GPA.of(student, student_scale))) is expected


def test_gpa_evidence_shows_normalized_values() -> None:
    rules = EligibilityRules(min_gpa=Decimal("85"), gpa_scale=Decimal("100"))
    gpa = next(r for r in evaluate_deterministic(rules, profile(), FUTURE, NOW) if r.rule == "gpa")
    assert "3.62/4.00" in gpa.evidence and "3.40/4.00" in gpa.evidence


@pytest.mark.parametrize(
    ("allowed", "nationality", "expected"),
    [
        (["Pakistan", "India"], "Pakistan", EligibilityOutcome.ELIGIBLE),
        (["Pakistani"], "pakistan", EligibilityOutcome.ELIGIBLE),
        (["USA", "UK"], "United States", EligibilityOutcome.ELIGIBLE),
        (["Germany"], "Pakistan", EligibilityOutcome.INELIGIBLE),
        (["International"], "Pakistan", EligibilityOutcome.ELIGIBLE),
        (["Kenya"], None, EligibilityOutcome.UNCERTAIN),
    ],
)
def test_nationality(
    allowed: list[str], nationality: str | None, expected: EligibilityOutcome
) -> None:
    assert (
        outcome(EligibilityRules(nationalities=allowed), profile(nationality=nationality))
        is expected
    )


def test_residency_and_graduation_window() -> None:
    rules = EligibilityRules(
        residency_countries=["Pakistan"], graduation_year_min=2026, graduation_year_max=2028
    )
    assert outcome(rules) is EligibilityOutcome.ELIGIBLE
    assert outcome(rules, profile(graduation_year=2030)) is EligibilityOutcome.INELIGIBLE
    assert outcome(rules, profile(country_of_residence="Canada")) is EligibilityOutcome.INELIGIBLE
    assert outcome(rules, profile(graduation_year=None)) is EligibilityOutcome.UNCERTAIN


def test_incomplete_profile_is_uncertain_not_eligible() -> None:
    empty = profile(degree_level=None, gpa=None)
    rules = EligibilityRules(degree_levels=[DegreeLevel.BACHELOR], min_gpa=Decimal("3"))
    assert outcome(rules, empty) is EligibilityOutcome.UNCERTAIN


def test_llm_results_combine_with_stage_one() -> None:
    base = evaluate_deterministic(EligibilityRules(), profile(), FUTURE, NOW)
    passing = RuleResult("leadership", RuleStatus.PASS, "team lead", RuleSource.LLM)
    failing = RuleResult("women only", RuleStatus.FAIL, "not stated", RuleSource.LLM)
    unknown = RuleResult("essay quality", RuleStatus.UNKNOWN, "cannot tell", RuleSource.LLM)
    assert decide([*base, passing]) is EligibilityOutcome.ELIGIBLE
    assert decide([*base, failing]) is EligibilityOutcome.INELIGIBLE
    assert decide([*base, unknown]) is EligibilityOutcome.UNCERTAIN


def test_rules_round_trip_and_snapshot_has_no_pii() -> None:
    rules = EligibilityRules(
        degree_levels=[DegreeLevel.BACHELOR],
        min_gpa=Decimal("3.2"),
        gpa_scale=Decimal("4"),
        nationalities=["Pakistan"],
        other_criteria=["Leadership"],
    )
    assert EligibilityRules.from_dict(rules.to_dict()) == rules
    assert EligibilityRules.from_dict(
        {"degree_levels": ["bachelor", "nonsense"], "min_gpa": "x"}
    ) == (EligibilityRules(degree_levels=[DegreeLevel.BACHELOR]))
    snap = profile_snapshot(profile())
    assert "+92 300" not in str(snap) and "phone" not in snap
    assert snap["gpa"] == "3.62/4"


def test_country_aliases() -> None:
    assert canonical_country(" U.K. ") == "united kingdom"
    assert canonical_country("Pakistani") == "pakistan"
