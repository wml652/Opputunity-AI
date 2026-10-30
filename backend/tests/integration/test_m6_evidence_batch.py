"""Milestone 6 applications: unified CV/profile evidence RAG answers a custom portal question
from an uploaded CV (document_chunks) without pausing - and still pauses when the CV lacks the
answer; the model never sees PII. Batch auto-stage fills several queued applications up to the
final OK gate and never submits."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser, register
from tests.fakes import FakeLLMFactory
from tests.fixtures import make_pdf
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import ESSAY, _queued_application

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)

PHONE = "+92 300 5550199"
NAME = "Zara Siddiqui"
CV_WITH_SKILLS = [
    f"{NAME} | zara.s@example.com | {PHONE}",
    "Education: BSc Computer Science, LUMS, GPA 3.62",
    "Programming languages: Python, Rust",
    "Projects: solar charger controller, weather station",
]
CV_WITHOUT_SKILLS = [
    f"{NAME} | zara.s@example.com | {PHONE}",
    "Education: BSc Computer Science, LUMS, GPA 3.62",
    "Projects: solar charger controller, weather station",
]
TERMINAL = {"AWAITING_FINAL_VERIFICATION", "PAUSED_NEEDS_USER_INPUT", "BLOCKED_NEEDS_HUMAN"}


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
async def aclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def _student(client: httpx.AsyncClient, cv_lines: list[str]) -> ApiUser:
    user = await register(client, f"zara-{uuid.uuid4().hex[:8]}@example.com")
    r = await user.client.put(
        "/api/v1/profile",
        json={
            "full_name": NAME,
            "pii": {"phone": PHONE},
            "degree_level": "BACHELOR",
            "institution": "LUMS",
            "major": "Computer Science",
            "gpa": 3.62,
            "gpa_scale": 4,
            "nationality": "Pakistan",
            "graduation_year": 2027,
        },
        headers={"x-csrf-token": user.csrf},
    )
    assert r.status_code == 200, r.text
    provider = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": "user/chat-model-x",
            "api_key": "nvapi-test-key-000000000000",
        },
    )
    assert provider.status_code == 201, provider.text
    up = await user.client.post(
        "/api/v1/documents",
        files={"file": ("cv.pdf", make_pdf(cv_lines), "application/pdf")},
        data={"kind": "CV"},
        headers={"x-csrf-token": user.csrf},
    )
    assert up.status_code == 201, up.text
    assert (await user.post("/api/v1/essays", json={"title": "Why CS", "body": ESSAY})).is_success
    return user


async def _wait(user: ApiUser, app_id: uuid.UUID, timeout: float = 150) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        if detail["application"]["state"] in TERMINAL:
            return detail
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"stuck: {detail['transitions'][-3:]}")
        await asyncio.sleep(0.25)


def _evidence_prompts(fake: FakeLLMFactory) -> list[str]:
    return [
        call[1].content
        for llm in fake.created
        for call in llm.calls
        if "You answer ONE question" in call[0].content
    ]


async def _portal_state(portal: FakePortalServer) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        state: dict[str, Any] = (await http.get(portal.base + "/__state")).json()
        return state


async def test_cv_evidence_answers_the_custom_question_without_pausing(
    aclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    fake_portal.state.reset()
    user = await _student(aclient, CV_WITH_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/apply", "Evidence Prog")
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    done = await _wait(user, app_id)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    assert done["questions"] == []
    langs = next(f for f in done["fields"] if f["field_key"] == "programming_languages")
    assert (langs["value"], langs["source"]) == ("Python, Rust", "DOCUMENT")
    assert langs["confidence"] >= 0.85
    assert "Programming languages: Python, Rust" in langs["evidence"]

    prompts = _evidence_prompts(fake_llm)
    assert prompts, "the evidence answerer was never consulted"
    for prompt in prompts:  # constitution: the model never receives PII
        for pii in (PHONE, "5550199", NAME, "Zara", "zara.s@example.com"):
            assert pii not in prompt
    assert PHONE not in langs["evidence"] and NAME not in langs["evidence"]
    assert (await _portal_state(fake_portal))["submissions"] == 0


async def test_still_pauses_when_the_cv_lacks_the_answer(
    aclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    fake_portal.state.reset()
    user = await _student(aclient, CV_WITHOUT_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/apply", "No Evidence")
    await user.post(f"/api/v1/applications/{app_id}/start")
    paused = await _wait(user, app_id)
    assert paused["application"]["state"] == "PAUSED_NEEDS_USER_INPUT", paused["transitions"]
    langs = next(f for f in paused["fields"] if f["field_key"] == "programming_languages")
    assert langs["value"] is None
    assert len(paused["questions"]) == 1
    assert _evidence_prompts(fake_llm)  # it looked before pausing


async def test_batch_auto_stage_prepares_many_and_never_submits(
    aclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    fake_portal.state.reset()
    user = await _student(aclient, CV_WITH_SKILLS)
    first = await _queued_application(infra, user, fake_portal.base + "/apply", "Batch One")
    second = await _queued_application(infra, user, fake_portal.base + "/apply?v=2", "Batch Two")
    third = await _queued_application(infra, user, fake_portal.base + "/apply", "Batch Three")

    bad = await user.post("/api/v1/applications/batch-prepare", json={"limit": 0})
    assert bad.status_code == 422
    started = await user.post("/api/v1/applications/batch-prepare", json={"limit": 2})
    assert started.status_code == 202, started.text
    ids = {uuid.UUID(i) for i in started.json()["started"]}
    assert ids == {first, second}  # top-2 by deadline/rank; the third stays queued

    for app_id in ids:
        ready = await _wait(user, app_id)
        assert ready["application"]["state"] == "AWAITING_FINAL_VERIFICATION", ready["transitions"]
        assert "SUBMIT_APPROVED" not in [t["to_state"] for t in ready["transitions"]]
    third_state = (await user.get(f"/api/v1/applications/{third}")).json()
    assert third_state["application"]["state"] == "QUEUED"
    portal = await _portal_state(fake_portal)
    assert portal["submissions"] == 0 and "POST /apply/submit" not in portal["hits"]
