"""Milestone 8 units: profile v2 eligibility rules, deterministic v2 field mapping, opportunity
ranking + sorting, the official-portal search picker and the live frame store."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.applications.use_cases import profile_v2_facts
from app.application.services.field_mapper import (
    PROFILE_V2_CONFIDENCE,
    FieldMapper,
    match_profile_key,
)
from app.domain.applications.filling import FieldSource, FormField
from app.domain.discovery.listing import pick_official_search_hit
from app.domain.discovery.models import AiPolicy, Opportunity
from app.domain.discovery.ranking import rank_opportunity, sort_items
from app.domain.eligibility.rules import (
    EligibilityRules,
    RuleStatus,
    evaluate_deterministic,
    profile_snapshot,
)
from app.domain.memory.models import RecallResult
from app.domain.profile.models import (
    GPA,
    DegreeLevel,
    EnglishProficiency,
    ProfileDetails,
    StudentPII,
    StudentProfile,
    WorkMode,
)
from app.domain.shared.enums import EligibilityOutcome, OpportunityType
from app.infrastructure.browser.live_frames import InMemoryLiveFrameStore

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _profile(**details: object) -> StudentProfile:
    return StudentProfile(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        full_name="Zara Siddiqui",
        pii=StudentPII.from_dict(
            {
                "phone": "+92 300 5550199",
                "city": "Lahore",
                "linkedin_url": "https://www.linkedin.com/in/zara-s",
                "github_url": "https://github.com/zaras",
                "referee_1_name": "Dr. Amina Khan",
                "referee_1_email": "amina.khan@lums.edu.pk",
                "referee_2_name": "Prof. Bilal Ahmed",
                "referee_2_email": "bilal@nust.edu.pk",
            }
        ),
        degree_level=DegreeLevel.BACHELOR,
        major="Computer Science",
        institution="LUMS",
        gpa=GPA.of(3.6, 4),
        nationality="Pakistan",
        country_of_residence="Pakistan",
        graduation_year=2027,
        skills=["Python", "Rust"],
        details=ProfileDetails.from_dict(
            {
                "english_proficiency_type": "MOI_EXEMPT",
                "research_interests": ["robotics", "computer vision"],
                "requires_visa_sponsorship": True,
                "has_valid_passport": True,
                "current_semester_or_year": "6th Semester / 3rd Year",
                "preferred_work_mode": "REMOTE",
                **details,
            }
        ),
    )


def _results(rules: EligibilityRules, profile: StudentProfile) -> dict[str, RuleStatus]:
    return {r.rule: r.status for r in evaluate_deterministic(rules, profile, None, NOW)}


def test_english_moi_and_test_scores() -> None:
    moi = _profile()
    assert (
        _results(EligibilityRules(english_tests={"IELTS": 6.5}, moi_accepted=True), moi)["english"]
        is RuleStatus.PASS
    )
    assert (
        _results(EligibilityRules(english_tests={"IELTS": 6.5}, moi_accepted=False), moi)["english"]
        is RuleStatus.FAIL
    )
    assert _results(EligibilityRules(english_tests={"IELTS": 6.5}), moi)["english"] is (
        RuleStatus.UNKNOWN
    )
    ielts = _profile(english_proficiency_type="IELTS", english_proficiency_score="7.0")
    assert _results(EligibilityRules(english_tests={"IELTS": 6.5}), ielts)["english"] is (
        RuleStatus.PASS
    )
    assert _results(EligibilityRules(english_tests={"IELTS": 7.5}), ielts)["english"] is (
        RuleStatus.FAIL
    )
    assert _results(EligibilityRules(), moi)["english"] is RuleStatus.PASS  # nothing required


def test_visa_passport_and_financial_need_rules() -> None:
    p = _profile()
    assert _results(EligibilityRules(visa_sponsorship=False), p)["visa"] is RuleStatus.FAIL
    assert _results(EligibilityRules(visa_sponsorship=True), p)["visa"] is RuleStatus.PASS
    no_visa = _profile(requires_visa_sponsorship=False)
    assert _results(EligibilityRules(visa_sponsorship=False), no_visa)["visa"] is RuleStatus.PASS
    no_passport = _profile(has_valid_passport=False)
    assert _results(EligibilityRules(requires_passport=True), no_passport)["passport"] is (
        RuleStatus.FAIL
    )
    assert _results(EligibilityRules(financial_need_required=True), p)["financial_need"] is (
        RuleStatus.UNKNOWN
    )


def test_llm_snapshot_carries_v2_facts_but_never_v2_pii() -> None:
    snap = profile_snapshot(_profile(publications_count=2))
    assert snap["research_interests"] == ["robotics", "computer vision"]
    assert snap["english_proficiency_type"] == "MOI_EXEMPT" and snap["publications_count"] == 2
    flat = repr(snap)
    for pii in ("linkedin", "github.com", "Lahore", "Amina", "amina.khan", "5550199"):
        assert pii not in flat


async def test_v2_labels_map_deterministically_at_095() -> None:
    profile = _profile(english_proficiency_type="IELTS", english_proficiency_score="7.5")
    facts = {
        **{k: v for k, v in profile.pii.to_dict().items() if v},
        **profile_v2_facts(profile.details),
    }

    async def recall(_: str) -> RecallResult:
        raise AssertionError("profile fields never reach memory recall")

    async def essay(_: FormField) -> None:
        return None

    mapper = FieldMapper(facts=facts, documents=[], recall=recall, essay=essay)
    expected = {
        "LinkedIn profile URL": "https://www.linkedin.com/in/zara-s",
        "GitHub": "https://github.com/zaras",
        "City": "Lahore",
        "Referee 1 Name": "Dr. Amina Khan",
        "Referee 1 Email": "amina.khan@lums.edu.pk",
        "Referee 2 Name": "Prof. Bilal Ahmed",
        "Second referee email": "bilal@nust.edu.pk",
        "Current Year/Semester": "6th Semester / 3rd Year",
        "English Proficiency (IELTS/TOEFL)": "IELTS 7.5",
        "Do you require visa sponsorship?": "Yes",
    }
    for i, (label, value) in enumerate(expected.items()):
        res = await mapper.resolve(FormField(f"f{i}", label, "text", f"[name=f{i}]"))
        assert (res.value, res.source, res.confidence) == (
            value,
            FieldSource.PROFILE,
            PROFILE_V2_CONFIDENCE,
        ), label
    assert match_profile_key("Phone") == "phone"  # v1 keys keep 0.97 / unchanged
    assert match_profile_key("High School / Secondary GPA") == "secondary_school_percentage"


def _opp(title: str, **kw: object) -> Opportunity:
    base = Opportunity(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        campaign_id=None,
        page_id=None,
        type=OpportunityType.RESEARCH,
        title=title,
        organization="Lab",
        location=None,
        is_international=True,
        deadline_at=NOW + timedelta(days=20),
        deadline_raw=None,
        portal_url="https://apply.example.edu/form",
        source_url="https://example.edu/news",
        summary="Undergraduate research in robotics and computer vision.",
        eligibility=EligibilityRules(degree_levels=[DegreeLevel.BACHELOR], work_mode="REMOTE"),
        ai_policy=AiPolicy.UNKNOWN,
        dedupe_key=title,
        embedding_model="t",
        latest_outcome=EligibilityOutcome.ELIGIBLE,
        created_at=NOW,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_match_score_breakdown_and_sorting() -> None:
    p = _profile()
    best = rank_opportunity(_opp("Robotics Vision Internship"), p, NOW)
    assert best.eligibility == 40 and best.portal == 17 and best.urgency == 12
    assert best.alignment == 23 and best.score == 92  # type 6 + research 12 + degree 3 + mode 2
    job = rank_opportunity(
        _opp(
            "Assistant BPS-15",
            type=OpportunityType.OTHER,
            summary="Government job.",
            portal_url="https://www.studyabroad.pk/jobs/x",
            latest_outcome=EligibilityOutcome.UNCERTAIN,
            eligibility=EligibilityRules(),
            deadline_at=None,
        ),
        p,
        NOW,
    )
    assert job.score < 40 and job.portal == 4 and "aggregator / blog page" in job.reasons
    items = [
        ("a", _opp("Alpha", latest_outcome=EligibilityOutcome.INELIGIBLE)),
        ("b", _opp("Bravo Robotics", deadline_at=NOW + timedelta(days=3))),
        ("c", _opp("Charlie", deadline_at=None)),
    ]
    ranked = [(k, o, rank_opportunity(o, p, NOW)) for k, o in items]

    def order(sort: str, direction: str) -> list[str]:
        out = sort_items(
            ranked,
            sort,
            direction,
            opportunity=lambda r: r[1],
            rank=lambda r: r[2],
            status=lambda r: r[1].latest_outcome.value if r[1].latest_outcome else "",
        )
        return [k for k, _, _ in out]

    assert order("match", "desc")[0] == "b" and order("match", "asc")[0] == "a"
    assert order("deadline", "asc") == ["b", "a", "c"]  # undated always last
    assert order("deadline", "desc") == ["a", "b", "c"]
    assert order("title", "asc") == ["a", "b", "c"] and order("title", "desc") == ["c", "b", "a"]


def test_official_search_hit_picker() -> None:
    hits = [
        ("https://www.studyabroad.pk/scholarships/omega", "Omega University Scholarship 2027"),
        ("https://www.facebook.com/omega", "Omega University Scholarship"),
        ("https://www.omega.edu/scholarships/international", "Omega University Scholarship"),
        ("https://www.unrelated.edu/apply", "Unrelated Fellowship"),
    ]
    pick = pick_official_search_hit(hits, "Omega University Scholarship", "Omega University")
    assert pick == "https://www.omega.edu/scholarships/international"
    assert pick_official_search_hit(hits[:2], "Omega University Scholarship", None) is None


async def test_live_frame_store_is_per_user_and_bounded() -> None:
    store = InMemoryLiveFrameStore(max_frames=2)
    alice, bob, app = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seq = await store.put(alice, app, b"\xff\xd8one")
    assert await store.get(alice, app) == (seq, b"\xff\xd8one")
    assert await store.get(bob, app) is None  # never another tenant's frame
    await store.put(alice, uuid.uuid4(), b"2")
    await store.put(alice, uuid.uuid4(), b"3")
    assert await store.get(alice, app) is None  # evicted (LRU)
    assert WorkMode.REMOTE.value == "REMOTE" and EnglishProficiency.NATIVE.value == "NATIVE"
    assert Decimal("1") == Decimal(1)
