""" "Sign In in Browser & Resume": a portal behind a login wall blocks the filler (it never asks
for or types a password); the user signs in in a visible browser (fake capturer here); the
cookies are stored encrypted for the portal domain and the application resumes with them up to
the final OK gate. Later runs on the same portal load the saved session automatically."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from app.domain.discovery.sessions import SessionKind
from app.infrastructure.scraping.fetchers import playwright_available
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import _queued_application
from tests.integration.test_m6_evidence_batch import CV_WITH_SKILLS, _portal_state, _student, _wait

pytestmark = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)


class PortalLoginCapturer:
    """Stands in for the visible browser: the 'user' signs in and the portal sets its cookie."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def capture(self, url: str, *, timeout_s: float, wait_for_close: bool = False) -> str:
        self.urls.append(url)
        cookie = {
            "name": "portal_auth",
            "value": "ok",
            "domain": "127.0.0.1",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        }
        return json.dumps({"cookies": [cookie], "origins": []})


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
async def uclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory
) -> AsyncIterator[tuple[httpx.AsyncClient, PortalLoginCapturer]]:
    capturer = PortalLoginCapturer()
    infra.session_capturer = capturer
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c, capturer
    infra.session_capturer = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def test_sign_in_in_browser_unblocks_and_resumes_with_saved_cookies(
    uclient: tuple[httpx.AsyncClient, PortalLoginCapturer],
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    fake_llm: FakeLLMFactory,
) -> None:
    client, capturer = uclient
    fake_portal.state.reset()
    user = await _student(client, CV_WITH_SKILLS)
    members = fake_portal.base + "/apply?v=members"
    app_id = await _queued_application(infra, user, members, "Members Scholarship")

    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    blocked = await _wait(user, app_id)
    assert blocked["application"]["state"] == "BLOCKED_NEEDS_HUMAN", blocked["transitions"]
    assert blocked["transitions"][-1]["reason"].startswith("[PORTAL_LOGIN_OR_REGISTER_REQUIRED]")
    assert blocked["questions"] == []  # never asks the student for a password
    assert all(f["field_type"] != "password" for f in blocked["fields"])
    inbox = (await user.get("/api/v1/inbox")).json()
    assert [b["application"]["id"] for b in inbox["blocked"]] == [str(app_id)]

    started = await user.post(f"/api/v1/applications/{app_id}/unblock")
    assert started.status_code == 202, started.text
    assert started.json()["domain"] == "127.0.0.1" and started.json()["url"] == members

    for _ in range(200):  # the capture runs in the background; wait for it to resume the app
        detail = (await user.get(f"/api/v1/applications/{app_id}")).json()
        if detail["application"]["state"] != "BLOCKED_NEEDS_HUMAN":
            break
        await asyncio.sleep(0.1)
    ready = await _wait(user, app_id)
    assert ready["application"]["state"] == "AWAITING_FINAL_VERIFICATION", ready["transitions"]
    states = [t["to_state"] for t in ready["transitions"]]
    assert states[-4:] == [
        "BLOCKED_NEEDS_HUMAN",
        "RESUME_QUEUED",
        "FILLING",
        "AWAITING_FINAL_VERIFICATION",
    ]
    assert capturer.urls == [members]

    async with infra.uow_factory(user.id) as uow:
        saved = await uow.browser_sessions.find(user.id, "127.0.0.1", SessionKind.PORTAL)
    assert saved is not None and b"portal_auth" not in saved.storage_state.ciphertext

    # A second application on the same portal is authenticated from the start.
    second = await _queued_application(infra, user, members, "Members Fellowship")
    await user.post(f"/api/v1/applications/{second}/start")
    done = await _wait(user, second)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]

    portal = await _portal_state(fake_portal)
    assert portal["hits"]["GET /apply login-wall"] == 1  # only the very first, unsigned visit
    assert portal["submissions"] == 0  # the final OK gate still stands

    again = await user.post(f"/api/v1/applications/{second}/unblock")
    assert again.status_code == 422  # only BLOCKED applications can be unblocked
    missing = await user.post(f"/api/v1/applications/{uuid.uuid4()}/unblock")
    assert missing.status_code == 404
