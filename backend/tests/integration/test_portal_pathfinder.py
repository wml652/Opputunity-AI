"""Milestone 7: official portal resolution, the 4-hop portal pathfinder and portal gates.

The fake portal runs on 127.0.0.1 (the "official university") and mirrors itself on 127.0.0.2,
which the test settings declare an aggregator host:
  - an aggregator article's portal URL resolves to the university's official page (discovery
    and the queue repair);
  - the filler never types on the aggregator, even into a form there, and walks
    Aggregator -> Overview -> How to apply -> (window.open popup, SPA-hydrated) form;
  - a Slate-style sign-in landing page, an email-only page and a closed page are classified."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_discovery_deps
from app.domain.shared.enums import ApplicationState
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


class LoginCapturer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def capture(self, url: str, *, timeout_s: float, wait_for_close: bool = False) -> str:
        self.urls.append(url)
        return json.dumps({"cookies": [], "origins": []})


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
async def pclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory, search_urls: list[str]
) -> AsyncIterator[tuple[httpx.AsyncClient, LoginCapturer]]:
    capturer = LoginCapturer()
    infra.session_capturer = capturer
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls), search_factory=None)
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c, capturer
    infra.session_capturer = None
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


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


async def _run(user: ApiUser, infra: Infrastructure, url: str, title: str) -> dict[str, Any]:
    app_id = await _queued_application(infra, user, url, title)
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    return await _settled(user, app_id)


async def test_campaign_resolves_the_official_portal_and_fills_through_four_hops(
    pclient: tuple[httpx.AsyncClient, LoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    search_urls: list[str],
) -> None:
    client, _ = pclient
    fake_portal.state.reset()
    article = fake_portal.agg_base + "/agg/article/omega-university-scholarship"
    search_urls[:] = [article]
    user = await _student(client, CV_WITH_SKILLS)
    created = await user.post(
        "/api/v1/campaigns",
        json={"keywords": ["scholarship"], "use_websites": False, "max_search_queries": 1},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    for _ in range(1_800):
        campaign = (await user.get(f"/api/v1/campaigns/{cid}")).json()["campaign"]
        if campaign["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        await asyncio.sleep(0.1)
    assert campaign["status"] == "COMPLETED", campaign

    (opp,) = (await user.get("/api/v1/opportunities")).json()
    assert opp["title"] == "Omega University Scholarship"
    assert opp["portal_url"] == fake_portal.base + "/uni/overview"  # not the aggregator post
    assert opp["source_url"] == article

    async with infra.uow_factory(user.id) as uow:
        ((app, _),) = await uow.app_details.list_by_states(
            user.id, [ApplicationState.AWAITING_FINAL_VERIFICATION]
        )
    detail = (await user.get(f"/api/v1/applications/{app.id}")).json()
    assert "/apply/step3" in detail["checkpoints"][-1]["url"]
    portal = await _portal_state(fake_portal)
    assert portal["hits"]["GET /uni/how-to-apply"] >= 1  # Overview -> How to apply -> popup
    assert portal["submissions"] == 0 and "POST /agg/keystroke" not in portal["hits"]


async def test_filler_never_types_on_an_aggregator_and_hops_off_site_to_the_form(
    pclient: tuple[httpx.AsyncClient, LoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, _ = pclient
    fake_portal.state.reset()
    user = await _student(client, CV_WITH_SKILLS)
    article = fake_portal.agg_base + "/agg/article/omega-university-scholarship"
    done = await _run(user, infra, article, "Omega University Scholarship")
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    keys = {f["field_key"] for f in done["fields"]}
    assert not keys & {"applicant_name", "applicant_email", "applicant_phone"}
    assert {"full_name", "programming_languages"} <= keys
    portal = await _portal_state(fake_portal)
    assert "POST /agg/keystroke" not in portal["hits"]  # not a single keystroke on the aggregator
    assert portal["hits"]["GET /uni/overview"] >= 1 and portal["submissions"] == 0

    # An aggregator page without any official link: blocked, still nothing typed.
    fake_portal.state.reset()
    blocked = await _run(user, infra, article + "?nolink=1", "Omega Without Link")
    assert blocked["application"]["state"] == "BLOCKED_NEEDS_HUMAN"
    assert blocked["application"]["last_error"].startswith("[AGGREGATOR_WITHOUT_OFFICIAL_LINK]")
    assert [f for f in blocked["fields"] if f["value"]] == []
    assert "POST /agg/keystroke" not in (await _portal_state(fake_portal))["hits"]


async def test_slate_sign_in_gate_is_classified_with_the_sign_in_url_ready(
    pclient: tuple[httpx.AsyncClient, LoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, capturer = pclient
    fake_portal.state.reset()
    user = await _student(client, CV_WITH_SKILLS)
    landing = fake_portal.base + "/slate/landing"
    done = await _run(user, infra, landing, "Omega Graduate Programme")
    assert done["application"]["state"] == "BLOCKED_NEEDS_HUMAN"
    reason = done["application"]["last_error"]
    assert reason.startswith("[PORTAL_LOGIN_OR_REGISTER_REQUIRED]")
    assert "Sign In in Browser & Resume" in reason
    checkpoint = done["checkpoints"][-1]
    assert checkpoint["url"] == fake_portal.base + "/slate/login" and checkpoint["has_screenshot"]
    assert [f for f in done["fields"] if f["value"]] == []

    started = await user.post(f"/api/v1/applications/{done['application']['id']}/unblock")
    assert started.status_code == 202, started.text
    assert started.json()["url"] == fake_portal.base + "/slate/login"
    for _ in range(100):
        if capturer.urls:
            break
        await asyncio.sleep(0.05)
    assert capturer.urls == [fake_portal.base + "/slate/login"]


async def test_email_only_and_closed_portals_are_explained(
    pclient: tuple[httpx.AsyncClient, LoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, _ = pclient
    user = await _student(client, CV_WITH_SKILLS)
    email = await _run(user, infra, fake_portal.base + "/uni/email-apply", "Omega Fellowship")
    assert email["application"]["state"] == "BLOCKED_NEEDS_HUMAN"
    reason = email["application"]["last_error"]
    assert reason.startswith("[EMAIL_OR_PACKAGE_SUBMISSION_ONLY]")
    assert "fellowships@omega.example.edu" in reason and "draft is ready" in reason
    assert any(d["prompt"].startswith("Statement of purpose") for d in email["drafts"])

    closed = await _run(user, infra, fake_portal.base + "/uni/closed", "Omega Summer School")
    reason = closed["application"]["last_error"]
    assert reason.startswith("[APPLICATIONS_NOT_OPEN_OR_CLOSED]")
    assert "Applications for the 2099 cycle are now closed" in reason


async def test_queue_repair_points_aggregator_applications_at_the_official_portal(
    pclient: tuple[httpx.AsyncClient, LoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    client, _ = pclient
    user = await _student(client, CV_WITH_SKILLS)
    article = fake_portal.agg_base + "/agg/article/omega-university-scholarship"
    no_link = article + "?nolink=1"
    await _queued_application(infra, user, article, "Omega University Scholarship")
    await _queued_application(infra, user, no_link, "Omega Elsewhere")
    await _queued_application(infra, user, fake_portal.base + "/apply", "Already Official")
    repaired = await user.post("/api/v1/opportunities/repair-portals")
    assert repaired.status_code == 200, repaired.text
    by_title = {r["title"]: r for r in repaired.json()}
    assert set(by_title) == {"Omega University Scholarship", "Omega Elsewhere"}
    assert by_title["Omega University Scholarship"]["new_url"] == fake_portal.base + "/uni/overview"
    assert by_title["Omega Elsewhere"]["new_url"] is None
    opps = {o["title"]: o for o in (await user.get("/api/v1/opportunities")).json()}
    assert opps["Omega University Scholarship"]["portal_url"] == fake_portal.base + "/uni/overview"
    assert opps["Omega Elsewhere"]["portal_url"] == no_link
