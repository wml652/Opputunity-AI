"""Milestone 8: streaming auto-apply with the live viewport, profile v2 (API, encryption, form
mapping), ranked + sortable opportunities / applications, and the official-portal search
fallback for aggregator-only opportunities."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_discovery_deps
from app.domain.discovery.models import AiPolicy, Opportunity, dedupe_key
from app.domain.eligibility.rules import EligibilityRules
from app.domain.llm.embedding import EMBEDDING_DIM
from app.domain.ports.discovery import SearchHit
from app.domain.shared.enums import EligibilityOutcome, OpportunityType
from app.domain.shared.ids import new_uuid7
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import _queued_application
from tests.integration.test_m6_discovery import FakeSearch
from tests.integration.test_m6_evidence_batch import CV_WITH_SKILLS, _portal_state, _student

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)
RESTING = {"AWAITING_FINAL_VERIFICATION", "PAUSED_NEEDS_USER_INPUT", "BLOCKED_NEEDS_HUMAN"}


class HitSearch:
    """Web search returning fixed hits (the auto-search fallback's provider)."""

    name = "fake-hits"

    def __init__(self) -> None:
        self.hits: list[SearchHit] = []
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[:max_results]


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
def search_urls() -> list[str]:
    return []


@pytest.fixture
async def m8(
    infra: Infrastructure, fake_llm: FakeLLMFactory, search_urls: list[str]
) -> AsyncIterator[tuple[httpx.AsyncClient, HitSearch]]:
    hits = HitSearch()
    infra.search_provider = hits
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls), search_factory=None)
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c, hits
    infra.search_provider = None
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def _events(infra: Infrastructure, user: ApiUser) -> list[Any]:
    async with infra.uow_factory(user.id) as uow:
        return await uow.events.list_since(user.id, 0, 10_000)


async def _settled(user: ApiUser, app_id: uuid.UUID, timeout: float = 150) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        state = detail["application"]["state"]
        if state in RESTING and detail["transitions"][-1]["to_state"] == state:
            return detail
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"stuck: {detail['transitions'][-3:]}")
        await asyncio.sleep(0.25)


async def test_eligible_application_fills_while_the_campaign_is_still_crawling(
    m8: tuple[httpx.AsyncClient, HitSearch],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    search_urls: list[str],
) -> None:
    client, _ = m8
    fake_portal.state.reset()
    search_urls[:] = [fake_portal.base + "/programme/zeta-summer-research"] + [
        f"{fake_portal.base}/slowpage/{i}" for i in range(3)
    ]
    user = await _student(client, CV_WITH_SKILLS)
    created = await user.post(
        "/api/v1/campaigns",
        json={"keywords": ["research programme"], "use_websites": False, "max_search_queries": 1},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    for _ in range(1_800):
        campaign = (await user.get(f"/api/v1/campaigns/{cid}")).json()["campaign"]
        if campaign["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        await asyncio.sleep(0.1)
    assert campaign["status"] == "COMPLETED", campaign

    events = await _events(infra, user)
    extracting = next(
        e.seq
        for e in events
        if e.type == "campaign.phase" and e.payload.get("phase") == "EXTRACTING"
    )
    started = next(e for e in events if e.type == "application.auto_started")
    app_id = uuid.UUID(started.payload["application_id"])
    filling = next(
        e.seq
        for e in events
        if e.type == "application.state"
        and e.payload.get("state") == "FILLING"
        and e.payload.get("application_id") == str(app_id)
    )
    assert filling < extracting  # filling began while slow pages were still being crawled
    steps = [
        e.payload
        for e in events
        if e.type == "application.live_step" and e.payload["application_id"] == str(app_id)
    ]
    assert any(s["phase_label"].startswith("Hop 1/4") for s in steps)
    assert any(s["phase_label"].startswith("Filling") and s["value_source"] for s in steps)
    assert steps[-1]["final"] and steps[-1]["progress_pct"] == 100.0
    assert all(s["opportunity_title"] == "Zeta Summer Research Programme" for s in steps)
    assert max(s["frame_seq"] for s in steps) > 0

    frame = await user.get(f"/api/v1/applications/{app_id}/live-frame?t=1")
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    assert frame.content.startswith(b"\xff\xd8") and frame.headers["cache-control"] == "no-store"
    other = await user.get(f"/api/v1/applications/{uuid.uuid4()}/live-frame")
    assert other.status_code == 404

    detail = (await user.get(f"/api/v1/applications/{app_id}")).json()
    assert detail["application"]["state"] == "AWAITING_FINAL_VERIFICATION"
    assert (await _portal_state(fake_portal))["submissions"] == 0


async def test_profile_v2_round_trip_encryption_and_portal_mapping(
    m8: tuple[httpx.AsyncClient, HitSearch],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, _ = m8
    user = await _student(client, CV_WITH_SKILLS)
    current = (await user.get("/api/v1/profile")).json()
    body = {
        **{k: current[k] for k in ("full_name", "degree_level", "major", "institution")},
        "gpa": 3.62,
        "gpa_scale": 4,
        "nationality": "Pakistan",
        "pii": {
            "phone": "+92 300 5550199",
            "city": "Lahore",
            "linkedin_url": "https://www.linkedin.com/in/zara-s",
            "github_url": "https://github.com/zaras",
            "referees": [
                {"name": "Dr. Amina Khan", "email": "amina.khan@lums.edu.pk", "title": "Professor"},
                {"name": "Prof. Bilal Ahmed", "email": "bilal@nust.edu.pk"},
            ],
            "emergency_contact": {"name": "Sana", "phone": "+92 300 1112223"},
        },
        "details": {
            "current_semester_or_year": "6th Semester",
            "english_proficiency_type": "IELTS",
            "english_proficiency_score": "7.5",
            "research_interests": ["robotics"],
            "requires_visa_sponsorship": True,
            "standardized_tests": {"gre": "320"},
        },
    }
    saved = await user.client.put("/api/v1/profile", json=body, headers={"x-csrf-token": user.csrf})
    assert saved.status_code == 200, saved.text
    got = (await user.get("/api/v1/profile")).json()
    assert got["pii"]["referees"][1]["email"] == "bilal@nust.edu.pk"
    assert got["pii"]["emergency_contact"]["name"] == "Sana"
    assert got["details"]["standardized_tests"] == {"GRE": "320"}
    assert got["details"]["english_proficiency_type"] == "IELTS"

    async with infra.uow_factory(user.id) as uow:
        stored = await uow.profiles.get(user.id)
    assert stored is not None and stored.pii is not None
    assert b"linkedin" not in stored.pii.ciphertext and b"Amina" not in stored.pii.ciphertext
    assert stored.profile.details.research_interests == ["robotics"]

    app_id = await _queued_application(
        infra, user, fake_portal.base + "/apply-v2", "Omega Research Programme"
    )
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    done = await _settled(user, app_id)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    fields = {f["field_key"]: f for f in done["fields"]}
    for key, value in {
        "linkedin": "https://www.linkedin.com/in/zara-s",
        "github": "https://github.com/zaras",
        "city": "Lahore",
        "ref1_name": "Dr. Amina Khan",
        "ref2_email": "bilal@nust.edu.pk",
        "english": "IELTS 7.5",
    }.items():
        assert (fields[key]["value"], fields[key]["source"]) == (value, "PROFILE"), key
        assert fields[key]["confidence"] == 0.95


async def _opportunity(
    infra: Infrastructure,
    user: ApiUser,
    title: str,
    outcome: EligibilityOutcome,
    deadline_days: int | None,
    portal: str,
) -> Opportunity:
    now = datetime.now(UTC)
    opp = Opportunity(
        id=new_uuid7(),
        user_id=user.id,
        campaign_id=None,
        page_id=None,
        type=OpportunityType.RESEARCH,
        title=title,
        organization="Lab",
        location=None,
        is_international=True,
        deadline_at=now + timedelta(days=deadline_days) if deadline_days is not None else None,
        deadline_raw=None,
        portal_url=portal,
        source_url=portal,
        summary="Research placement.",
        eligibility=EligibilityRules(),
        ai_policy=AiPolicy.UNKNOWN,
        dedupe_key=dedupe_key(title, "Lab", None),
        embedding_model="test",
        latest_outcome=outcome,
        created_at=now,
    )
    async with infra.uow_factory(user.id) as uow:
        assert await uow.opportunities.add(opp, [1.0] + [0.0] * (EMBEDDING_DIM - 1))
        await uow.opportunities.set_outcome(user.id, opp.id, outcome)  # as the runner does
        await uow.commit()
    return opp


async def test_match_score_and_ascending_descending_sorting(
    m8: tuple[httpx.AsyncClient, HitSearch], infra: Infrastructure
) -> None:
    client, _ = m8
    user = await _student(client, CV_WITH_SKILLS)
    await _opportunity(
        infra, user, "Alpha Grant", EligibilityOutcome.INELIGIBLE, 10, "https://a.example.edu/x"
    )
    await _opportunity(
        infra, user, "Bravo Grant", EligibilityOutcome.ELIGIBLE, 5, "https://b.example.edu/x"
    )
    await _opportunity(
        infra, user, "Charlie Grant", EligibilityOutcome.UNCERTAIN, None, "https://studyabroad.pk/c"
    )

    async def titles(path: str) -> list[str]:
        res = await user.get(path)
        assert res.status_code == 200, res.text
        rows = res.json()
        assert all(isinstance(r["match_score"], int) for r in rows)
        return [r["title"] if "title" in r else r["opportunity"]["title"] for r in rows]

    assert await titles("/api/v1/opportunities?sort=match&order=desc") == [
        "Bravo Grant",
        "Charlie Grant",
        "Alpha Grant",
    ]
    assert await titles("/api/v1/opportunities?sort=match&order=asc") == [
        "Alpha Grant",
        "Charlie Grant",
        "Bravo Grant",
    ]
    assert await titles("/api/v1/opportunities?sort=deadline&order=asc") == [
        "Bravo Grant",
        "Alpha Grant",
        "Charlie Grant",
    ]
    assert (await titles("/api/v1/opportunities?sort=title&order=desc"))[0] == "Charlie Grant"
    bad = await user.get("/api/v1/opportunities?sort=popularity")
    assert bad.status_code == 422
    rows = (await user.get("/api/v1/opportunities?sort=match")).json()
    assert rows[0]["rank_breakdown"]["eligibility"] == 40

    a1 = await _queued_application(infra, user, "https://x.example.edu/apply", "Queue Late")
    a2 = await _queued_application(infra, user, "https://studyabroad.pk/y", "Queue Aggregator")
    apps = (await user.get("/api/v1/applications?sort=match&order=desc")).json()
    ids = [a["application_id"] for a in apps]
    assert ids.index(str(a1)) < ids.index(str(a2))  # official portal outranks aggregator
    asc = (await user.get("/api/v1/applications?sort=match&order=asc")).json()
    assert [a["application_id"] for a in asc] == list(reversed(ids))
    queue = (await user.get("/api/v1/applications/queue?sort=title&order=asc")).json()
    assert [q["opportunity"]["title"] for q in queue] == ["Queue Aggregator", "Queue Late"]


async def test_aggregator_only_opportunity_is_resolved_by_web_search_then_filled(
    m8: tuple[httpx.AsyncClient, HitSearch],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, hits = m8
    fake_portal.state.reset()
    hits.hits = [
        SearchHit("Omega University Scholarship 2027", fake_portal.agg_base + "/agg/x", "", "t"),
        SearchHit("Omega University Scholarship", "https://www.facebook.com/omega", "", "t"),
        SearchHit(
            "Omega University Scholarship - official page",
            fake_portal.base + "/uni/overview",
            "",
            "t",
        ),
    ]
    user = await _student(client, CV_WITH_SKILLS)
    no_link = fake_portal.agg_base + "/agg/article/omega-university-scholarship?nolink=1"
    app_id = await _queued_application(infra, user, no_link, "Omega University Scholarship")
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    done = await _settled(user, app_id)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    assert done["opportunity"]["portal_url"] == fake_portal.base + "/uni/overview"
    assert hits.queries and hits.queries[0].startswith('"Omega University Scholarship"')
    resolved = [e for e in await _events(infra, user) if e.type == "opportunity.portal_resolved"]
    assert resolved[-1].payload["via"] == "search" and resolved[-1].payload["old"] == no_link
    portal = await _portal_state(fake_portal)
    assert "POST /agg/keystroke" not in portal["hits"] and portal["submissions"] == 0
