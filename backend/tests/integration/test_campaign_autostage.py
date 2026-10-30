"""Continuous campaigns: auto-stage is on by default, the campaign moves through FILLING (and
AWAITING_OK) before COMPLETED, the filler hops from an overview page's "Apply Now" button to the
real 3-step form without touching the page's lead / newsletter forms, and both progress bars
(current process + overall) are streamed as `campaign.progress` events and reach 100%."""

from __future__ import annotations

import asyncio
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
from tests.integration.test_m6_evidence_batch import (
    CV_WITH_SKILLS,
    _portal_state,
    _student,
    _wait,
)

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)


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
async def sclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory, search_urls: list[str]
) -> AsyncIterator[httpx.AsyncClient]:
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls), search_factory=None)
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def _campaign_events(infra: Infrastructure, user: ApiUser, cid: str) -> list[Any]:
    async with infra.uow_factory(user.id) as uow:
        events = await uow.events.list_since(user.id, 0, 5_000)
    return [e for e in events if e.payload.get("campaign_id") == cid]


async def test_campaign_hops_landing_page_auto_fills_and_reports_progress(
    sclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    search_urls: list[str],
) -> None:
    fake_portal.state.reset()
    search_urls[:] = [fake_portal.base + "/programme/zeta-summer-research"]
    user = await _student(sclient, CV_WITH_SKILLS)
    created = await user.post(
        "/api/v1/campaigns",
        json={  # auto_stage is NOT passed: it is on by default
            "keywords": ["research programme"],
            "scope": "INTERNATIONAL",
            "use_websites": False,
            "max_search_queries": 1,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["settings"]["auto_stage"] is True
    assert created.json()["settings"]["auto_stage_limit"] == 10
    cid = created.json()["id"]

    detail: dict[str, Any] = {}
    for _ in range(1_800):
        detail = (await user.get(f"/api/v1/campaigns/{cid}")).json()
        if detail["campaign"]["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        await asyncio.sleep(0.1)
    campaign = detail["campaign"]
    assert campaign["status"] == "COMPLETED", campaign
    assert campaign["progress"]["overall_pct"] == 100.0
    assert campaign["progress"]["current"]["label"] == "Awaiting your OK"

    events = await _campaign_events(infra, user, cid)
    phases = [e.payload["phase"] for e in events if e.type == "campaign.phase"]
    assert phases == ["CRAWLING", "EXTRACTING", "FILLING", "AWAITING_OK", "COMPLETED"]
    progress = [e.payload for e in events if e.type == "campaign.progress"]
    filling = [p for p in progress if p["phase"] == "FILLING"]
    assert filling and filling[0]["current"]["label"] == "Auto-filling applications"
    assert filling[0]["stage_target"] == 1 and filling[-1]["current"]["pct"] == 100.0
    assert any("Zeta Summer Research Programme" in (p["current"]["detail"] or "") for p in filling)
    assert all(0 <= p["overall_pct"] < 100 for p in progress)
    assert progress[-1]["overall_pct"] > progress[0]["overall_pct"]
    staged = next(e for e in events if e.type == "applications.auto_staged")
    assert staged.payload["states"] == {"AWAITING_FINAL_VERIFICATION": 1}

    async with infra.uow_factory(user.id) as uow:
        staged_apps = await uow.app_details.list_by_states(
            user.id, [ApplicationState.AWAITING_FINAL_VERIFICATION]
        )
    ((app, opp),) = staged_apps
    assert opp.title == "Zeta Summer Research Programme"
    assert opp.portal_url == fake_portal.base + "/programme/zeta-summer-research"
    app_detail = (await user.get(f"/api/v1/applications/{app.id}")).json()
    assert app_detail["application"]["state"] == "AWAITING_FINAL_VERIFICATION"
    assert "/apply/step3" in app_detail["checkpoints"][-1]["url"]  # hopped to the real form
    keys = {f["field_key"] for f in app_detail["fields"]}
    assert {"full_name", "programming_languages", "cv_file"} <= keys
    # lead forms (in a form, formless widget, newsletter) are never filled
    assert not keys & {"StudentName", "Mobile", "LeadName", "LeadMobile", "newsletter_email"}

    portal = await _portal_state(fake_portal)
    assert portal["hits"]["GET /programme"] >= 2  # crawled once, opened by the filler
    assert "POST /lead" not in portal["hits"] and portal["submissions"] == 0


async def test_directory_page_with_only_an_inquiry_widget_is_never_staged(
    sclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    fake_portal.state.reset()
    user = await _student(sclient, CV_WITH_SKILLS)
    app_id = await _queued_application(
        infra, user, fake_portal.base + "/directory/all", "Directory Listing"
    )
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    done = await _wait(user, app_id)
    assert done["application"]["state"] == "BLOCKED_NEEDS_HUMAN", done["transitions"]
    assert "no application form found" in done["transitions"][-1]["reason"]
    assert [f for f in done["fields"] if f["value"]] == []  # the student's details went nowhere
    assert done["checkpoints"][-1]["url"].endswith("/directory/all")
