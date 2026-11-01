"""Milestone 10: DOM-RAG and imitation-learning data.

1. The agent fails on portal A (an entry link no keyword list or model understands) - that
   decision is logged as a failed trajectory.
2. The student teaches portal A in Teach Mode - the demonstration becomes a trajectory and,
   because the bot failed on that same page, a DPO correction pair.
3. On portal B (another host, different markup and CSS classes, same step) the agent recalls the
   taught step by DOM similarity and acts on it without any model call (Tier 2).
4. SFT / DPO exports contain only {{PROFILE.*}} tokens - no name, e-mail or phone."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from app.infrastructure.browser.teach_recorder import PlaywrightTeachRecorder
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import _queued_application
from tests.integration.test_m6_evidence_batch import CV_WITH_SKILLS, NAME, PHONE, _student

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)
RESTING = {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN", "PAUSED_NEEDS_USER_INPUT"}


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
async def tclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory
) -> AsyncIterator[httpx.AsyncClient]:
    async def student_on_portal_a(page: Any) -> None:
        await page.get_by_text("Commence the process").click()
        await page.wait_for_url("**/talent/form")
        await page.fill("#tn", NAME)
        await page.fill("#te", "zara.s@example.com")
        await page.fill("#tp", PHONE)
        await page.dispatch_event("#tp", "change")

    infra.teach_recorder = PlaywrightTeachRecorder(
        infra.crawl_guard, headless=True, driver=student_on_portal_a
    )
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


async def _settled(user: ApiUser, app_id: uuid.UUID) -> dict[str, Any]:
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        state = detail["application"]["state"]
        if state in RESTING and detail["transitions"][-1]["to_state"] == state:
            return detail
        await asyncio.sleep(0.25)
    raise AssertionError(detail["transitions"][-3:])


async def _events(infra: Infrastructure, user: ApiUser, type_: str) -> list[dict[str, Any]]:
    async with infra.uow_factory(user.id) as uow:
        events = await uow.events.list_since(user.id, 0, 10_000)
    return [e.payload for e in events if e.type == type_]


async def test_taught_step_on_portal_a_drives_portal_b_and_exports_are_pii_free(
    tclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
) -> None:
    fake_portal.state.reset()
    user = await _student(tclient, CV_WITH_SKILLS)
    portal_a = fake_portal.base + "/talent/start"
    app_a = await _queued_application(infra, user, portal_a, "Global Talent Programme (A)")

    # 1. Untaught: the agent cannot understand "Commence the process" and gets blocked.
    assert (await user.post(f"/api/v1/applications/{app_a}/start")).status_code == 202
    first = await _settled(user, app_a)
    assert first["application"]["state"] == "BLOCKED_NEEDS_HUMAN"
    stats = (await user.get("/api/v1/training/overview")).json()["stats"]
    assert stats["by_source"].get("auto_failure", 0) >= 1  # its own failed decision, logged

    # 2. Teach Mode on portal A.
    assert (await user.post(f"/api/v1/applications/{app_a}/teach")).status_code == 202
    for _ in range(600):
        if await _events(infra, user, "agent.workflow_saved"):
            break
        assert not await _events(infra, user, "agent.teach_failed")
        await asyncio.sleep(0.1)
    overview = (await user.get("/api/v1/training/overview?limit=200")).json()
    teach = [t for t in overview["trajectories"] if t["source"] == "teach"]
    click = next(t for t in teach if t["chosen_action"]["action"] == "click")
    assert click["chosen_action"]["label"] == "Commence the process"
    assert click["rejected_action"] is not None  # the bot's failed decision: a DPO pair
    fills = {
        t["chosen_action"]["label"]: t["chosen_action"]
        for t in teach
        if t["chosen_action"]["action"] == "fill"
    }
    assert fills["Full name"]["value_token"] == "{{PROFILE.FULL_NAME}}"
    assert fills["Mobile number"]["value_token"] == "{{PROFILE.PHONE}}"
    assert overview["stats"]["dpo_pairs"] >= 1

    # 3. Portal B: another host, a <button> with other CSS classes - recalled, no model call.
    portal_b = fake_portal.b_base + "/talent-b/start"
    app_b = await _queued_application(infra, user, portal_b, "Global Talent Programme (B)")
    assert (await user.post(f"/api/v1/applications/{app_b}/start")).status_code == 202
    done = await _settled(user, app_b)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    steps = [
        s["phase_label"]
        for s in await _events(infra, user, "application.live_step")
        if s["application_id"] == str(app_b)
    ]
    assert "Recalled taught step from 127.0.0.1" in steps
    assert not any(label.startswith("Agent decided") for label in steps)  # no LLM needed
    stats = (await user.get("/api/v1/training/overview")).json()["stats"]
    assert stats["rag_decisions"] >= 1 and stats["rag_hit_rate"] > 0

    # 4. Backfill + exports: tokens only, never personal values.
    filled = await user.post("/api/v1/training/backfill")
    assert filled.status_code == 200 and filled.json()["from_fields"] >= 1
    for fmt in ("sft", "dpo"):
        res = await user.get(f"/api/v1/training/export?format={fmt}")
        assert res.status_code == 200, res.text
        assert res.headers["content-disposition"] == f'attachment; filename="{fmt}.jsonl"'
        body = res.text
        rows = [json.loads(line) for line in body.splitlines() if line]
        assert rows, fmt
        for leaked in (NAME, "Zara", "Siddiqui", "zara.s@example.com", "5550199", PHONE):
            assert leaked not in body, (fmt, leaked)
        assert res.headers["x-dropped-records"] == "0"
    sft = (await user.get("/api/v1/training/export?format=sft")).text
    assert "{{PROFILE.FULL_NAME}}" in sft and "{{PROFILE.PHONE}}" in sft
    dpo = [
        json.loads(line)
        for line in (await user.get("/api/v1/training/export?format=dpo")).text.splitlines()
        if line
    ]
    assert any(not r["metadata"]["synthetic"] for r in dpo)  # a real human correction
    assert all(r["chosen"] != r["rejected"] for r in dpo)

    kit = await user.get("/api/v1/training/kit?file=notebook")
    assert kit.status_code == 200 and json.loads(kit.content)["nbformat"] == 4
