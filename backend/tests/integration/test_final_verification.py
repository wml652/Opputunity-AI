"""Milestone 5 end to end on the 3-step fake portal (real Chromium):

* the portal is NEVER submitted without the human OK,
* a stale `preview_hash` is rejected (409) and nothing is submitted,
* user edits are applied on the page and verified before Submit; the receipt screenshot and
  confirmation text are saved; a second approval / run never submits twice,
* reject cancels without submitting; a value the page will not hold re-stages for review,
* deadline sweep, lease reaper, security headers, opt-in LinkedIn cap."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.application.applications.submission import ReapLeases, RunSubmission, SweepDeadlines
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_application_deps
from app.domain.discovery.sessions import BrowserSession, SessionKind
from app.domain.ports.discovery import Listing
from app.domain.shared.enums import ApplicationState
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import CUSTOM_QUESTION, FakePortalServer
from tests.integration.test_application_filler import _queued_application, _student, _wait_state

needs_browser = pytest.mark.skipif(
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
async def mclient(
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


async def _portal_state(portal: FakePortalServer) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        data: dict[str, Any] = (await http.get(portal.base + "/__state")).json()
    return data


async def _ready_for_ok(
    user: ApiUser, infra: Infrastructure, portal: FakePortalServer, query: str = ""
) -> uuid.UUID:
    """Fill a fake-portal application up to AWAITING_FINAL_VERIFICATION."""
    app_id = await _queued_application(
        infra, user, portal.base + "/apply" + query, f"Program {uuid.uuid4().hex[:6]}"
    )
    assert (await user.post(f"/api/v1/applications/{app_id}/start")).status_code == 202
    state = await _wait_state(
        user,
        app_id,
        {"PAUSED_NEEDS_USER_INPUT", "AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN"},
    )
    if state["application"]["state"] == "PAUSED_NEEDS_USER_INPUT":
        (question,) = (await user.get("/api/v1/inbox")).json()["questions"]
        assert question["question"] == CUSTOM_QUESTION
        answered = await user.post(
            f"/api/v1/inbox/questions/{question['id']}/answer",
            json={"answer": "Python, TypeScript", "save_to_memory": True},
        )
        assert answered.status_code == 200
        state = await _wait_state(
            user, app_id, {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN"}
        )
    assert state["application"]["state"] == "AWAITING_FINAL_VERIFICATION", state["transitions"]
    return app_id


@needs_browser
async def test_ok_gate_stale_hash_edits_receipt_and_no_double_submit(
    mclient: httpx.AsyncClient, infra: Infrastructure, fake_portal: FakePortalServer
) -> None:
    fake_portal.state.reset()
    user = await _student(mclient, infra)
    app_id = await _ready_for_ok(user, infra, fake_portal)
    before = await _portal_state(fake_portal)
    assert before["submissions"] == 0 and "POST /apply/submit" not in before["hits"]  # no OK yet

    preview = (await user.get(f"/api/v1/applications/{app_id}/verification")).json()
    assert len(preview["preview_hash"]) == 64
    assert preview["checkpoint"]["has_screenshot"] is True
    fields = {f["field_key"]: f for f in preview["fields"]}
    assert fields["phone"]["value"] == "+92 300 5550199"
    assert fields["programming_languages"]["value"] == "Python, TypeScript"

    stale = await user.post(
        f"/api/v1/applications/{app_id}/approve",
        json={"preview_hash": "0" * 64, "edits": {"phone": "+92 300 1111111"}},
    )
    assert stale.status_code == 409 and "changed since you reviewed" in stale.text
    assert (await _portal_state(fake_portal))["submissions"] == 0
    still = (await user.get(f"/api/v1/applications/{app_id}")).json()
    assert still["application"]["state"] == "AWAITING_FINAL_VERIFICATION"

    edited_phone = "+92 321 7654321"
    ok = await user.post(
        f"/api/v1/applications/{app_id}/approve",
        json={"preview_hash": preview["preview_hash"], "edits": {"phone": edited_phone}},
    )
    assert ok.status_code == 202, ok.text
    assert ok.json()["edited_fields"] == ["phone"] and ok.json()["outcome"] == "APPROVED"
    done = await _wait_state(
        user, app_id, {"SUBMITTED", "SUBMISSION_FAILED", "AWAITING_FINAL_VERIFICATION"}
    )
    assert done["application"]["state"] == "SUBMITTED", done["transitions"]
    states = [t["to_state"] for t in done["transitions"]]
    assert states[-3:] == ["SUBMIT_APPROVED", "SUBMITTING", "SUBMITTED"]

    portal = await _portal_state(fake_portal)
    assert portal["submissions"] == 1
    phone_field = next(f for f in done["fields"] if f["field_key"] == "phone")
    assert phone_field["value"] == edited_phone and phone_field["source"] == "USER"

    record = (await user.get(f"/api/v1/applications/{app_id}/verification")).json()
    latest = record["latest"]
    assert latest["outcome"] == "SUBMITTED" and latest["has_receipt"] is True
    assert latest["observed_hash"] == latest["approved_preview_hash"]
    assert "Thank you" in (record["receipt_text"] or "")
    png = await user.get(f"/api/v1/applications/{app_id}/receipt")
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")

    again = await user.post(
        f"/api/v1/applications/{app_id}/approve",
        json={"preview_hash": record["preview_hash"], "edits": {}},
    )
    assert again.status_code == 409  # nothing to approve: already submitted
    assert await RunSubmission(build_application_deps(infra)).execute(user.id, app_id) is None
    assert (await _portal_state(fake_portal))["submissions"] == 1  # never submitted twice


@needs_browser
async def test_submitted_values_are_the_edited_ones(
    mclient: httpx.AsyncClient, infra: Infrastructure, fake_portal: FakePortalServer
) -> None:
    fake_portal.state.reset()
    user = await _student(mclient, infra)
    app_id = await _ready_for_ok(user, infra, fake_portal)
    preview = (await user.get(f"/api/v1/applications/{app_id}/verification")).json()
    await user.post(
        f"/api/v1/applications/{app_id}/approve",
        json={
            "preview_hash": preview["preview_hash"],
            "edits": {"programming_languages": "Python, Rust", "university": "LUMS Lahore"},
        },
    )
    await _wait_state(
        user, app_id, {"SUBMITTED", "SUBMISSION_FAILED", "AWAITING_FINAL_VERIFICATION"}
    )
    (submission,) = fake_portal.state.submissions
    draft = submission["draft"]
    assert draft["step1"]["university"] == "LUMS Lahore"  # an EARLIER step was re-filled
    assert draft["step2"]["programming_languages"] == "Python, Rust"
    assert submission["cv_bytes"] > 0  # the CV upload was attached


@needs_browser
async def test_reject_cancels_and_unholdable_value_restages(
    mclient: httpx.AsyncClient, infra: Infrastructure, fake_portal: FakePortalServer
) -> None:
    fake_portal.state.reset()
    user = await _student(mclient, infra)
    first = await _ready_for_ok(user, infra, fake_portal)
    preview = (await user.get(f"/api/v1/applications/{first}/verification")).json()
    rejected = await user.post(
        f"/api/v1/applications/{first}/reject",
        json={"preview_hash": preview["preview_hash"], "reason": "not this year"},
    )
    assert rejected.status_code == 204
    detail = (await user.get(f"/api/v1/applications/{first}")).json()
    assert detail["application"]["state"] == "CANCELLED"
    assert (await _portal_state(fake_portal))["submissions"] == 0

    second = await _ready_for_ok(user, infra, fake_portal, "?v=2")
    preview = (await user.get(f"/api/v1/applications/{second}/verification")).json()
    too_long = "x" * 3_500  # the portal's essay textarea holds at most 3000 characters
    await user.post(
        f"/api/v1/applications/{second}/approve",
        json={"preview_hash": preview["preview_hash"], "edits": {"motivation_essay": too_long}},
    )
    back = await _wait_state(
        user, second, {"AWAITING_FINAL_VERIFICATION", "SUBMITTED", "SUBMISSION_FAILED"}, 90
    )
    assert back["application"]["state"] == "AWAITING_FINAL_VERIFICATION", back["transitions"]
    record = (await user.get(f"/api/v1/applications/{second}/verification")).json()
    assert record["latest"]["outcome"] == "RESTAGED"
    assert any(m["field"] == "motivation_essay" for m in record["latest"]["mismatches"])
    assert (await _portal_state(fake_portal))["submissions"] == 0  # re-staged, NOT submitted


async def test_deadline_sweep_and_lease_reaper(
    mclient: httpx.AsyncClient, infra: Infrastructure
) -> None:
    user = await _student(mclient, infra)
    expired = await _queued_application(infra, user, "https://portal.example.org/a", "Old one")
    filling = await _queued_application(infra, user, "https://portal.example.org/b", "Stuck")
    submitting = await _queued_application(infra, user, "https://portal.example.org/c", "Mid")
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with infra.uow_factory(user.id) as uow:
        for app_id, states in (
            (filling, [ApplicationState.PREPARING, ApplicationState.FILLING]),
            (
                submitting,
                [
                    ApplicationState.PREPARING,
                    ApplicationState.FILLING,
                    ApplicationState.AWAITING_FINAL_VERIFICATION,
                    ApplicationState.SUBMIT_APPROVED,
                    ApplicationState.SUBMITTING,
                ],
            ),
        ):
            found = await uow.app_details.get(user.id, app_id)
            assert found is not None
            app = found[0]
            for target in states:
                expected = app.version
                app.state, app.version = target, app.version + 1
                assert await uow.app_details.save_state(user.id, app, expected)
            await uow.app_details.set_lease(user.id, app_id, "dead-worker", past)
        found = await uow.app_details.get(user.id, expired)
        assert found is not None
        await uow.session.execute(  # type: ignore[attr-defined]
            __import__("sqlalchemy").text("UPDATE applications SET deadline_at = :d WHERE id = :i"),
            {"d": past, "i": expired},
        )
        await uow.commit()
    assert await ReapLeases(infra.system_uow_factory, infra.clock).execute() == 2
    assert await SweepDeadlines(infra.system_uow_factory, infra.clock).execute() >= 1
    states = {
        app_id: (await user.get(f"/api/v1/applications/{app_id}")).json()["application"]["state"]
        for app_id in (expired, filling, submitting)
    }
    assert states == {
        expired: "EXPIRED",
        filling: "BLOCKED_NEEDS_HUMAN",
        submitting: "SUBMISSION_FAILED",  # never retried blindly: it may have gone through
    }


async def test_security_headers(mclient: httpx.AsyncClient) -> None:
    r = await mclient.get("/api/v1/health")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cross-origin-resource-policy"] == "same-site"


async def test_linkedin_daily_cap_is_enforced(
    mclient: httpx.AsyncClient, infra: Infrastructure
) -> None:
    """The signed-in LinkedIn adapter is opt-in and stops at the user's daily action cap."""
    from dataclasses import replace

    from app.application.discovery.use_cases import RunCampaign
    from app.bootstrap.container import build_discovery_deps
    from app.domain.discovery.sessions import BROWSER_SESSION_AAD
    from app.domain.ports.crypto import aad
    from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner

    calls: list[str] = []

    class FakeLinkedIn:
        async def search(
            self, keyword: str, location: str | None, limit: int, storage_state: str
        ) -> list[Listing]:
            assert "li_at" in storage_state  # the user's decrypted session is used
            calls.append(keyword)
            return []

    deps = replace(build_discovery_deps(infra), linkedin_auth=FakeLinkedIn())
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        user = await _student(client, infra)
        blocked = await user.post(
            "/api/v1/campaigns",
            json={
                "auto_stage": False,
                "keywords": ["a"],
                "use_websites": False,
                "use_search": False,
                "use_linkedin_auth": True,
            },
        )
        assert blocked.status_code == 422 and "LinkedIn" in blocked.json()["error"]["message"]

        now = datetime.now(UTC)
        row_id = uuid.uuid4()
        session = BrowserSession(
            id=row_id,
            user_id=user.id,
            domain="linkedin.com",
            kind=SessionKind.LINKEDIN,
            storage_state=infra.cipher.encrypt(
                b'{"cookies": [{"name": "li_at", "value": "x"}], "origins": []}',
                aad(BROWSER_SESSION_AAD, "storage_state", user.id, row_id),
            ),
            created_at=now,
            tos_acknowledged_at=now,
            expires_at=now + timedelta(days=1),
            daily_action_cap=1,
        )
        async with infra.uow_factory(user.id) as uow:
            await uow.browser_sessions.upsert(session)
            await uow.commit()
        created = await user.post(
            "/api/v1/campaigns",
            json={
                "auto_stage": False,
                "keywords": ["research internship", "undergraduate scholarship"],
                "use_websites": False,
                "use_search": False,
                "use_linkedin_auth": True,
            },
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        status = ""
        for _ in range(200):
            status = (await user.get(f"/api/v1/campaigns/{cid}")).json()["campaign"]["status"]
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            await asyncio.sleep(0.05)
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None
    assert status == "COMPLETED"
    assert calls == ["research internship"]  # cap 1: the second keyword was not searched
    async with infra.uow_factory(user.id) as uow:
        stored = await uow.browser_sessions.find(user.id, "linkedin.com", SessionKind.LINKEDIN)
    assert stored is not None and stored.actions_today == 1
