"""Milestone 9: the learning agent.

* Teach Mode: a scripted "student" (in place of a human in the visible window) applies on a
  portal; the recorder learns the route and the answers - custom answers go to semantic memory,
  personal fields only as profile paths - and a self-submitted application is recorded as
  SUBMITTED by the user. Without a submit, the agent resumes alone with what it learned.
* LLM agent loop: a portal whose buttons no keyword list understands ("Begin my journey",
  "Onwards", "Lodge my application"), behind a cookie banner and an invisible reCAPTCHA, is
  filled up to the final review - and the final submit is never clicked."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from app.domain.shared.enums import ApplicationState
from app.infrastructure.browser.teach_recorder import PlaywrightTeachRecorder
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import _queued_application
from tests.integration.test_m6_evidence_batch import (
    CV_WITH_SKILLS,
    CV_WITHOUT_SKILLS,
    _portal_state,
    _student,
)

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)
QUESTION = "Which programming languages are you most comfortable with?"
Drive = Callable[[Any], Awaitable[None]]


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
def driver_box() -> dict[str, Drive]:
    return {}


@pytest.fixture
async def bclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory, driver_box: dict[str, Drive]
) -> AsyncIterator[httpx.AsyncClient]:
    async def drive(page: Any) -> None:
        await driver_box["drive"](page)

    infra.teach_recorder = PlaywrightTeachRecorder(infra.crawl_guard, headless=True, driver=drive)
    infra.teach_session = None
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c
    infra.teach_recorder = None
    infra.teach_session = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def _events(infra: Infrastructure, user: ApiUser, type_: str) -> list[dict[str, Any]]:
    async with infra.uow_factory(user.id) as uow:
        events = await uow.events.list_since(user.id, 0, 10_000)
    return [e.payload for e in events if e.type == type_]


async def _wait_event(infra: Infrastructure, user: ApiUser, type_: str) -> dict[str, Any]:
    for _ in range(600):
        found = await _events(infra, user, type_)
        if found:
            return found[-1]
        failed = await _events(infra, user, "agent.teach_failed")
        assert not failed, failed
        await asyncio.sleep(0.1)
    raise AssertionError(f"no {type_} event")


async def _wait_state(user: ApiUser, app_id: uuid.UUID, states: set[str]) -> dict[str, Any]:
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        state = detail["application"]["state"]
        if state in states and detail["transitions"][-1]["to_state"] == state:
            return detail
        await asyncio.sleep(0.25)
    raise AssertionError(f"stuck in {detail['application']['state']}: {detail['transitions'][-3:]}")


async def _walk_to_review(page: Any) -> None:
    """What the student does by hand on the odd portal, up to the review page."""
    await page.get_by_role("button", name="Accept all").click()
    await page.get_by_role("button", name="Begin my journey").click()
    await page.wait_for_url("**/odd/form")
    await page.fill("#fn", "Zara Siddiqui")
    await page.fill("#em", "zara@example.com")
    await page.fill("#q", "Python, Haskell")
    await page.dispatch_event("#q", "change")
    await page.get_by_role("button", name="Onwards").click()
    await page.wait_for_url("**/odd/review")


async def test_teach_mode_learns_the_route_and_answers_and_records_a_self_submission(
    bclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    driver_box: dict[str, Drive],
) -> None:
    fake_portal.state.reset()

    async def student(page: Any) -> None:
        await _walk_to_review(page)
        await page.get_by_role("button", name="Lodge my application").click()
        await page.wait_for_url("**/odd/lodge")

    driver_box["drive"] = student
    user = await _student(bclient, CV_WITHOUT_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/odd/start", "Omega Talent")
    started = await user.post(f"/api/v1/applications/{app_id}/teach")
    assert started.status_code == 202, started.text
    assert started.json()["url"] == fake_portal.base + "/odd/start"

    saved = await _wait_event(infra, user, "agent.workflow_saved")
    assert saved["domain"] == "127.0.0.1" and saved["submitted"] is True
    assert saved["answers_learned"] == 1 and saved["navigation_steps"] >= 2

    steps = await _events(infra, user, "agent.learned_step")
    by_label = {s["label"]: s for s in steps if s["kind"] == "fill"}
    assert by_label["Full name"]["value_preview"].startswith("(personal")  # never shown/stored
    assert by_label[QUESTION]["value_preview"] == "Python, Haskell"
    assert any(s["kind"] == "click" and s["label"] == "Begin my journey" for s in steps)

    async with infra.uow_factory(user.id) as uow:
        workflow = await uow.agent_workflows.find(user.id, "127.0.0.1")
    assert workflow is not None
    names = [s.locator.name for s in workflow.navigation_steps]
    assert "Begin my journey" in names and "Accept all" in names
    mappings = {m.field_name: m for m in workflow.field_mappings}
    assert mappings["full_name"].mapped_profile_path == "full_name"
    assert mappings["full_name"].user_value is None  # personal values are never stored
    assert mappings["email"].user_value is None
    assert mappings["langs"].memory_qa_id and mappings["langs"].user_value == "Python, Haskell"

    memory = (await user.get("/api/v1/memory")).json()["items"]
    assert any(m["question"] == QUESTION and m["answer"] == "Python, Haskell" for m in memory)
    assert not any("Zara" in m["answer"] or "zara@" in m["answer"] for m in memory)

    done = await _wait_state(user, app_id, {"SUBMITTED"})
    last = done["transitions"][-1]
    assert last["actor"] == "USER" and "yourself" in last["reason"]
    workflows = (await user.get("/api/v1/agent/workflows")).json()
    assert workflows[0]["domain"] == "127.0.0.1" and workflows[0]["learned_answers"] == 1


async def test_after_teaching_without_submit_the_agent_finishes_alone_with_what_it_learned(
    bclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    driver_box: dict[str, Drive],
) -> None:
    fake_portal.state.reset()
    driver_box["drive"] = _walk_to_review
    user = await _student(bclient, CV_WITHOUT_SKILLS)  # the CV cannot answer the question
    teach_app = await _queued_application(
        infra, user, fake_portal.base + "/odd/start", "Omega Talent (taught)"
    )
    assert (await user.post(f"/api/v1/applications/{teach_app}/teach")).status_code == 202
    saved = await _wait_event(infra, user, "agent.workflow_saved")
    assert saved["submitted"] is False

    done = await _wait_state(
        user,
        teach_app,
        {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN", "PAUSED_NEEDS_USER_INPUT"},
    )
    trail = [
        s["phase_label"]
        for s in await _events(infra, user, "application.live_step")
        if s["application_id"] == str(teach_app)
    ]
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", (
        trail,
        done["checkpoints"],
    )
    langs = next(f for f in done["fields"] if f["field_key"] == "langs")
    assert (langs["value"], langs["source"]) == ("Python, Haskell", "MEMORY")  # learned answer
    steps = [
        s["phase_label"]
        for s in await _events(infra, user, "application.live_step")
        if s["application_id"] == str(teach_app)
    ]
    assert any(label.startswith("Replaying learned step") for label in steps)
    portal = await _portal_state(fake_portal)
    assert "POST /odd/lodge" not in portal["hits"] and portal["submissions"] == 0


async def test_llm_agent_navigates_unusual_labels_and_stops_before_the_final_submit(
    bclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    fake_portal.state.reset()
    user = await _student(bclient, CV_WITH_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/odd/start", "Omega Odd")
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    done = await _wait_state(
        user,
        app_id,
        {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN", "PAUSED_NEEDS_USER_INPUT"},
    )
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    assert "/odd/review" in done["checkpoints"][-1]["url"]
    fields = {f["field_key"]: f for f in done["fields"]}
    assert fields["full_name"]["source"] == "PROFILE" and fields["langs"]["value"] == "Python, Rust"
    steps = [
        s["phase_label"]
        for s in await _events(infra, user, "application.live_step")
        if s["application_id"] == str(app_id)
    ]
    assert any(label == "Agent decided: click" for label in steps)
    assert any(label == "Agent decided: reached final review" for label in steps)
    portal = await _portal_state(fake_portal)
    assert portal["hits"]["POST /odd/form"] >= 1  # "Onwards" was understood
    assert "POST /odd/lodge" not in portal["hits"] and portal["submissions"] == 0
    # The navigator saw the page, never the student's personal data.
    prompts = [
        call[1].content
        for llm in fake_llm.created
        for call in llm.calls
        if "You operate a web browser" in call[0].content
    ]
    assert prompts and not any("Zara" in p or "5550199" in p for p in prompts)
    assert ApplicationState.AWAITING_FINAL_VERIFICATION.value == "AWAITING_FINAL_VERIFICATION"
