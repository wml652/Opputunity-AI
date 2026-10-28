"""Milestone 4 end to end against the 3-step fake portal (real Chromium via Playwright):
fill -> pause on unknown question -> answer in Inbox (learned into memory) -> resume with ledger
replay after a wiped draft -> stop before Submit; a second portal with the same question is then
answered from memory without pausing. Plus essay drafts and notifications."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from app.domain.applications.models import Application
from app.domain.discovery.models import AiPolicy, Opportunity, dedupe_key
from app.domain.eligibility.rules import EligibilityRules
from app.domain.llm.embedding import EMBEDDING_DIM
from app.domain.shared.enums import ApplicationState, NotifStatus, OpportunityType
from app.domain.shared.ids import new_uuid7
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser, register
from tests.fakes import FakeLLMFactory
from tests.fixtures import make_pdf
from tests.fixtures.fake_portal.app import CUSTOM_QUESTION, FakePortalServer

ESSAY = (
    "I grew up in a house where the radio was always on. My father fixed them for neighbours, and "
    "I learned to listen before I learned to read. However, listening was not enough.\n\n"
    "At fifteen I built my first receiver. It failed. I rebuilt it twice; the third version picked "
    "up a station from Karachi, faint but real. That night taught me patience.\n\n"
    "Today I study computer science because it feels like the same work: tracing a signal through "
    "noise until it becomes something a person can use."
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


async def _student(client: httpx.AsyncClient, infra: Infrastructure) -> ApiUser:
    user = await register(client, "ayesha@example.com")
    r = await user.client.put(
        "/api/v1/profile",
        json={
            "full_name": "Ayesha Khan",
            "pii": {"phone": "+92 300 5550199"},
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
    assert (
        await user.post(
            "/api/v1/llm/providers",
            json={
                "kind": "NVIDIA_NIM",
                "model": "user/chat-model-x",
                "api_key": "nvapi-test-key-000000000000",
            },
        )
    ).status_code == 201
    up = await user.client.post(
        "/api/v1/documents",
        files={"file": ("cv.pdf", make_pdf(["Ayesha Khan CV"]), "application/pdf")},
        data={"kind": "CV"},
        headers={"x-csrf-token": user.csrf},
    )
    assert up.status_code == 201, up.text
    essay = await user.post("/api/v1/essays", json={"title": "Why CS", "body": ESSAY})
    assert essay.status_code == 201, essay.text
    return user


async def _queued_application(
    infra: Infrastructure, user: ApiUser, portal_url: str, title: str
) -> uuid.UUID:
    now = datetime.now(UTC)
    opp = Opportunity(
        id=new_uuid7(),
        user_id=user.id,
        campaign_id=None,
        page_id=None,
        type=OpportunityType.RESEARCH,
        title=title,
        organization="Research Lab",
        location=None,
        is_international=True,
        deadline_at=datetime(2099, 1, 1, tzinfo=UTC),
        deadline_raw=None,
        portal_url=portal_url,
        source_url=portal_url,
        summary="Fixture program",
        eligibility=EligibilityRules(),
        ai_policy=AiPolicy.UNKNOWN,
        dedupe_key=dedupe_key(title, "Research Lab", None),
        embedding_model="test",
        latest_outcome=None,
        created_at=now,
    )
    app = Application(
        id=new_uuid7(),
        user_id=user.id,
        opportunity_id=opp.id,
        state=ApplicationState.QUEUED,
        priority_rank=1,
        deadline_at=opp.deadline_at,
        created_at=now,
        updated_at=now,
    )
    async with infra.uow_factory(user.id) as uow:
        assert await uow.opportunities.add(opp, [1.0] + [0.0] * (EMBEDDING_DIM - 1))
        assert await uow.applications.add(app)
        await uow.commit()
    return app.id


async def _wait_state(
    user: ApiUser, app_id: uuid.UUID, states: set[str], timeout: float = 120
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        if detail["application"]["state"] in states:
            return detail
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"stuck in {detail['application']['state']}: {detail['transitions'][-3:]}"
            )
        await asyncio.sleep(0.25)


async def _verified_email_channel(user: ApiUser, infra: Infrastructure) -> None:
    saved = await user.post(
        "/api/v1/notifications/channels", json={"channel": "EMAIL", "address": "ayesha@example.com"}
    )
    assert saved.status_code == 201, saved.text
    log = Path(infra.settings.logs_dir) / "notifications.log"
    code = next(
        json.loads(line)["text"].split("code is ")[1][:6]
        for line in reversed(log.read_text(encoding="utf-8").splitlines())
        if "code is" in line
    )
    bad = await user.post(
        f"/api/v1/notifications/channels/{saved.json()['id']}/verify", json={"code": "000000"}
    )
    assert bad.status_code == 422
    ok = await user.post(
        f"/api/v1/notifications/channels/{saved.json()['id']}/verify", json={"code": code}
    )
    assert ok.status_code == 200 and ok.json()["verified"] is True


async def test_fill_pause_answer_resume_replay_and_learn(
    aclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    fake_portal.state.reset()
    user = await _student(aclient, infra)
    await _verified_email_channel(user, infra)
    app_id = await _queued_application(
        infra, user, fake_portal.base + "/apply", "Fixture Research Program"
    )

    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    paused = await _wait_state(user, app_id, {"PAUSED_NEEDS_USER_INPUT", "BLOCKED_NEEDS_HUMAN"})
    assert paused["application"]["state"] == "PAUSED_NEEDS_USER_INPUT", paused["transitions"]
    fields = {f["field_key"]: f for f in paused["fields"]}
    assert (
        fields["full_name"]["value"] == "Ayesha Khan" and fields["full_name"]["source"] == "PROFILE"
    )
    assert fields["email"]["value"] == "ayesha@example.com"
    assert fields["phone"]["value"] == "+92 300 5550199"
    assert fields["level"]["value"] == "Undergraduate (Bachelor)"
    assert fields["motivation_essay"]["source"] == "ESSAY" and fields["motivation_essay"]["value"]
    assert fields["programming_languages"]["value"] is None
    assert (
        "nickname" in fields and fields["nickname"]["value"] is None
    )  # optional: left blank, not asked
    draft = paused["drafts"][0]
    assert draft["critique"]["cliches"] == [] and draft["critique"].get("revised") is True
    assert 0 < draft["voice_match_score"] <= 1 and 0 <= draft["burstiness_score"] <= 1
    portal_draft = (await httpx.AsyncClient().get(fake_portal.base + "/__state")).json()
    assert portal_draft["drafts"][0]["step1"]["full_name"] == "Ayesha Khan"
    assert portal_draft["drafts"][0]["step1"]["level"] == "ug"

    checkpoint = paused["checkpoints"][-1]
    assert checkpoint["step_index"] == 1 and checkpoint["has_screenshot"]
    png = await user.get(f"/api/v1/applications/checkpoints/{checkpoint['id']}/screenshot")
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")

    inbox = (await user.get("/api/v1/inbox")).json()
    (question,) = inbox["questions"]
    assert (
        question["question"] == CUSTOM_QUESTION
        and question["opportunity_title"] == "Fixture Research Program"
    )
    assert (await user.get("/api/v1/inbox/summary")).json()["open_questions"] == 1

    # The portal session expires while the student is away.
    async with httpx.AsyncClient() as http:
        await http.post(fake_portal.base + "/__wipe")
    answered = await user.post(
        f"/api/v1/inbox/questions/{question['id']}/answer",
        json={"answer": "Python, TypeScript", "save_to_memory": True},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["resumed"] is True and answered.json()["memory_qa_id"]

    ready = await _wait_state(user, app_id, {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN"})
    assert ready["application"]["state"] == "AWAITING_FINAL_VERIFICATION", ready["transitions"]
    states = [t["to_state"] for t in ready["transitions"]]
    assert states == [
        "PREPARING",
        "FILLING",
        "PAUSED_NEEDS_USER_INPUT",
        "RESUME_QUEUED",
        "FILLING",
        "AWAITING_FINAL_VERIFICATION",
    ]
    portal = (await httpx.AsyncClient().get(fake_portal.base + "/__state")).json()
    assert portal["hits"]["POST /apply/step1"] == 2  # replayed after the wipe
    final = portal["drafts"][-1]
    assert final["step2"]["programming_languages"] == "Python, TypeScript"
    assert "solar charger" in final["step2"]["motivation_essay"]
    assert (
        portal["submissions"] == 0 and "POST /apply/submit" not in portal["hits"]
    )  # never submitted
    assert ready["checkpoints"][-1]["step_index"] == 2

    memory = (await user.get("/api/v1/memory")).json()["items"]
    assert any(
        m["question"] == CUSTOM_QUESTION and m["answer"] == "Python, TypeScript" for m in memory
    )

    recent = (await user.get("/api/v1/notifications")).json()["recent"]
    templates = {(n["template"], n["status"]) for n in recent}
    assert ("question_needed", "SENT") in templates and ("ready_for_ok", "SENT") in templates

    # A second portal asking the same question is answered from memory - no pause.
    second = await _queued_application(
        infra, user, fake_portal.base + "/apply?v=2", "Second Research Program"
    )
    await user.post(f"/api/v1/applications/{second}/start")
    done = await _wait_state(
        user,
        second,
        {"AWAITING_FINAL_VERIFICATION", "PAUSED_NEEDS_USER_INPUT", "BLOCKED_NEEDS_HUMAN"},
    )
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    assert done["questions"] == []
    langs = next(f for f in done["fields"] if f["field_key"] == "programming_languages")
    assert (langs["value"], langs["source"]) == ("Python, TypeScript", "MEMORY")
    assert langs["confidence"] >= 0.85
    assert fake_llm.models_used == {"user/chat-model-x"}


async def test_essay_drafts_api_and_ai_policy(
    aclient: httpx.AsyncClient, infra: Infrastructure, fake_llm: FakeLLMFactory
) -> None:
    user = await _student(aclient, infra)
    created = await user.post(
        "/api/v1/essays/drafts", json={"prompt": "Why this program?", "word_limit": 200}
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert "delve" not in draft["body"].lower() and draft["critique"]["cliches"] == []
    assert draft["status"] == "CRITIQUED" and draft["model"] == "user/chat-model-x"
    edited = await user.client.put(
        f"/api/v1/essays/drafts/{draft['id']}",
        json={"body": draft["body"] + " I mean it."},
        headers={"x-csrf-token": user.csrf},
    )
    assert edited.json()["status"] == "USER_EDITED"

    from app.application.essay.synthesis import EssayRequest
    from app.bootstrap.container import build_essay_engine

    prohibited = await build_essay_engine(infra).execute(
        user.id, EssayRequest(prompt="Describe your goals", ai_policy=AiPolicy.PROHIBITED)
    )
    assert prohibited.requires_manual and "prohibits" in prohibited.critique["policy"]


async def test_notification_dedupe_and_signed_webhooks(
    aclient: httpx.AsyncClient, infra: Infrastructure
) -> None:
    user = await register(aclient, "notify@example.com")
    await _verified_email_channel(user, infra)
    from app.application.notifications.use_cases import NotifyUser
    from app.bootstrap.container import build_notification_deps
    from app.domain.notification.models import Template

    notify = NotifyUser(build_notification_deps(infra))
    app_id = await _queued_application(
        infra, user, "https://portal.example.org/apply", "Notify Program"
    )
    first = await notify.execute(
        user.id, Template.READY_FOR_OK, {"title": "X"}, application_id=app_id, state="READY:1"
    )
    again = await notify.execute(
        user.id, Template.READY_FOR_OK, {"title": "X"}, application_id=app_id, state="READY:1"
    )
    assert first[0].status is NotifStatus.SENT and not first[0].deduplicated
    assert again[0].deduplicated  # same template + application + state is never re-sent

    # Signed delivery receipts update the notification; unsigned ones are rejected.
    import base64
    import hashlib
    import hmac

    infra.settings = infra.settings.model_copy(
        update={
            "twilio_auth_token": SecretStr("tw-secret"),
            "meta_app_secret": SecretStr("meta-secret"),
            "public_api_url": "http://testserver",
        }
    )
    app = create_app_with(infra, run_relay=False)
    async with infra.uow_factory(user.id) as uow:
        sent = next(
            n
            for n in await uow.notifications.recent(user.id, 10)
            if n.template is Template.READY_FOR_OK
        )
        await uow.notifications.update_result(
            user.id, sent.id, NotifStatus.SENT, "twilio", "SM123", None
        )
        await uow.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        params = {"MessageSid": "SM123", "MessageStatus": "delivered"}
        url = "http://testserver/api/v1/webhooks/twilio"
        sig = base64.b64encode(
            hmac.new(
                b"tw-secret", (url + "MessageSidSM123MessageStatusdelivered").encode(), hashlib.sha1
            ).digest()
        ).decode()
        assert (
            await http.post(
                "/api/v1/webhooks/twilio", data=params, headers={"X-Twilio-Signature": "bad"}
            )
        ).status_code == 401
        assert (
            await http.post(
                "/api/v1/webhooks/twilio", data=params, headers={"X-Twilio-Signature": sig}
            )
        ).status_code == 204
        body = json.dumps(
            {
                "entry": [
                    {"changes": [{"value": {"statuses": [{"id": "SM123", "status": "failed"}]}}]}
                ]
            }
        ).encode()
        meta_sig = "sha256=" + hmac.new(b"meta-secret", body, hashlib.sha256).hexdigest()
        assert (
            await http.post(
                "/api/v1/webhooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=0"}
            )
        ).status_code == 401
        delivered = (await user.get("/api/v1/notifications")).json()["recent"]
        assert any(n["status"] == "DELIVERED" for n in delivered)
        assert (
            await http.post(
                "/api/v1/webhooks/meta", content=body, headers={"X-Hub-Signature-256": meta_sig}
            )
        ).status_code == 204
    failed = (await user.get("/api/v1/notifications")).json()["recent"]
    assert any(n["status"] == "FAILED" and n["template"] == "ready_for_ok" for n in failed)
