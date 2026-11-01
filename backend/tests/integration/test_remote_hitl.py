"""Track 2: remote human-in-the-loop browsers (servers have no screen) + Track 1 fill resume.

* A fill whose worker died resumes from its saved state and still stops at the review gate.
* RemoteView: JPEG frames stream from a headless Chromium; mouse / keyboard / paste / back /
  finish / close events from the console drive it.
* Teach Mode end to end with the browser driven ONLY through the remote input channel (what the
  console sends over the WebSocket): the workflow, PII-free trajectories and memory are saved.
* "Sign In & Resume" remote: the portal opens as a stream; "Done" closes it, the cookies are
  stored and the application resumes.
* The WebSocket: Origin check, cookie auth, tenant isolation, meta + frames out, validated input
  in, "closed" when the session ends."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from app.api.cookies import ACCESS_COOKIE
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_resume
from app.domain.ports.hitl import TEACH, HitlSession
from app.infrastructure.browser.remote_view import RemoteView
from app.infrastructure.browser.teach_recorder import PlaywrightTeachRecorder
from app.infrastructure.scraping.fetchers import playwright_available
from tests.conftest import ApiUser, DatabaseUrls, register
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.integration.test_application_filler import _queued_application
from tests.integration.test_m6_evidence_batch import CV_WITH_SKILLS, NAME, PHONE, _student

needs_browser = pytest.mark.skipif(
    not playwright_available(), reason="Chromium for Playwright is not installed"
)
RESTING = {"AWAITING_FINAL_VERIFICATION", "BLOCKED_NEEDS_HUMAN", "PAUSED_NEEDS_USER_INPUT"}
EMAIL = "zara.s@example.com"


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
async def holder() -> dict[str, Any]:
    return {}


@pytest.fixture
async def rclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory, holder: dict[str, Any]
) -> AsyncIterator[httpx.AsyncClient]:
    """The app as on a server: HITL_MODE=remote (headless browsers streamed to the console)."""
    previous = infra.settings.hitl_mode
    infra.settings.hitl_mode = "remote"
    infra.teach_session = None
    infra.session_capturer = None
    infra.teach_recorder = PlaywrightTeachRecorder(
        infra.crawl_guard,
        headless=True,
        driver=lambda page: _console_drives_teach(infra, holder, page),
        hub=infra.hitl_hub,
        slots=infra.browser_slots,
    )
    app = create_app_with(infra, run_relay=False)
    holder["app"] = app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as c:
        yield c
    infra.settings.hitl_mode = previous
    infra.teach_recorder = None
    infra.teach_session = None
    if infra.application_runner is not None:
        await infra.application_runner.aclose()  # type: ignore[attr-defined]
        infra.application_runner = None


async def _settled(user: ApiUser, app_id: uuid.UUID) -> dict[str, Any]:
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/applications/{app_id}")).json()
        state = detail["application"]["state"]
        last = detail["transitions"][-1]["to_state"] if detail["transitions"] else None
        if state in RESTING and last == state:
            return detail
        await asyncio.sleep(0.25)
    raise AssertionError(detail["transitions"][-3:])


async def _events(infra: Infrastructure, user_id: uuid.UUID, type_: str) -> list[dict[str, Any]]:
    async with infra.uow_factory(user_id) as uow:
        events = await uow.events.list_since(user_id, 0, 10_000)
    return [e.payload for e in events if e.type == type_]


async def _session_of(infra: Infrastructure, user_id: uuid.UUID) -> HitlSession:
    assert infra.hitl_hub is not None
    for _ in range(200):
        sessions = await infra.hitl_hub.list(user_id)
        if sessions:
            return sessions[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no remote session opened")


async def _first_frame(infra: Infrastructure, user_id: uuid.UUID, sid: uuid.UUID) -> bytes:
    assert infra.hitl_hub is not None
    for _ in range(200):
        frame = await infra.hitl_hub.latest_frame(user_id, sid)
        if frame:
            return frame
        await asyncio.sleep(0.05)
    raise AssertionError("no frame was streamed")


async def _console_drives_teach(infra: Infrastructure, holder: dict[str, Any], page: Any) -> None:
    """The person in front of the console: every action goes through the remote input channel
    (exactly what the viewer sends over the WebSocket). `page` is only read for coordinates."""
    hub = infra.hitl_hub
    assert hub is not None
    uid: uuid.UUID = holder["user"].id
    session = await _session_of(infra, uid)
    holder["frame"] = await _first_frame(infra, uid, session.id)

    async def click(locator: Any) -> None:
        box = await locator.bounding_box()
        assert box is not None
        await hub.send_input(
            uid,
            session.id,
            {
                "type": "mouse",
                "action": "click",
                "x": box["x"] + box["width"] / 2,
                "y": box["y"] + box["height"] / 2,
            },
        )

    await click(page.get_by_text("Commence the process"))
    await page.wait_for_url("**/talent/form", timeout=20_000)
    for selector, text in (("#tn", NAME), ("#te", EMAIL), ("#tp", PHONE)):
        await click(page.locator(selector))
        await hub.send_input(uid, session.id, {"type": "text", "text": text})
        for _ in range(100):
            if await page.input_value(selector) == text:
                break
            await asyncio.sleep(0.05)
        assert await page.input_value(selector) == text
    for action in ("down", "up"):  # Tab out of the last field: its change event fires
        await hub.send_input(uid, session.id, {"type": "key", "action": action, "key": "Tab"})
    await asyncio.sleep(1.0)  # debounced field events reach the recorder


@needs_browser
async def test_interrupted_fill_resumes_and_still_stops_at_review(
    rclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    clean_db: DatabaseUrls,
) -> None:
    fake_portal.state.reset()
    user = await _student(rclient, CV_WITH_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/apply", "Resumed Prog")
    conn = await asyncpg.connect(clean_db.owner)
    try:  # a worker picked it up and died mid-fill
        await conn.execute(
            "UPDATE applications SET state = 'FILLING', lock_owner = 'dead-host:7:browser/x', "
            "lease_expires_at = now() - interval '1 minute' WHERE id = $1",
            app_id,
        )
    finally:
        await conn.close()
    report = await build_resume(infra).execute()
    assert report.fills_resumed == 1
    done = await _settled(user, app_id)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    conn = await asyncpg.connect(clean_db.owner)
    try:
        row = await conn.fetchrow(
            "SELECT resume_count, lock_owner FROM applications WHERE id = $1", app_id
        )
    finally:
        await conn.close()
    assert row["resume_count"] == 1 and row["lock_owner"] is None
    assert fake_portal.state.submissions == []  # never submitted


@needs_browser
async def test_remote_view_streams_frames_and_applies_console_input(
    infra: Infrastructure, fake_portal: FakePortalServer
) -> None:
    from playwright.async_api import async_playwright

    hub = infra.hitl_hub
    assert hub is not None
    uid = uuid.uuid4()
    session = HitlSession(uuid.uuid4(), uid, TEACH, "Talent", fake_portal.base + "/talent/start")
    await hub.open(session)
    finished: list[bool] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        view = RemoteView(hub, session, on_finish=lambda: finished.append(True))
        await view.attach(context, page)
        try:
            await page.goto(fake_portal.base + "/talent/start")
            frame = await _first_frame(infra, uid, session.id)
            assert frame[:2] == b"\xff\xd8"  # a JPEG of the viewport
            box = await page.get_by_text("Commence the process").bounding_box()
            assert box is not None
            await hub.send_input(
                uid,
                session.id,
                {"type": "mouse", "action": "click", "x": box["x"] + 5, "y": box["y"] + 5},
            )
            await page.wait_for_url("**/talent/form", timeout=20_000)
            await hub.send_input(uid, session.id, {"type": "nav", "action": "back"})
            await page.wait_for_url("**/talent/start", timeout=20_000)
            await hub.send_input(uid, session.id, {"type": "finish"})
            for _ in range(100):
                if finished:
                    break
                await asyncio.sleep(0.05)
            assert finished == [True]
            await hub.send_input(uid, session.id, {"type": "close"})  # "Done" in the console
            for _ in range(100):
                if not [p for p in context.pages if not p.is_closed()]:
                    break
                await asyncio.sleep(0.05)
            assert not [p for p in context.pages if not p.is_closed()]
        finally:
            await view.detach()
            await browser.close()
    await hub.close(uid, session.id)


@needs_browser
async def test_remote_teach_mode_driven_from_the_console(
    rclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    holder: dict[str, Any],
) -> None:
    fake_portal.state.reset()
    user = await _student(rclient, CV_WITH_SKILLS)
    holder["user"] = user
    app_id = await _queued_application(
        infra, user, fake_portal.base + "/talent/start", "Global Talent Programme"
    )
    started = await user.post(f"/api/v1/applications/{app_id}/teach")
    assert started.status_code == 202, started.text
    session_id = started.json()["session_id"]
    assert session_id is not None
    listed = (await user.get("/api/v1/hitl/sessions")).json()
    assert [s["id"] for s in listed] == [session_id] and listed[0]["kind"] == TEACH
    opened = await _events(infra, user.id, "hitl.session_opened")
    assert opened and opened[-1]["id"] == session_id

    for _ in range(600):
        if await _events(infra, user.id, "agent.workflow_saved"):
            break
        assert not await _events(infra, user.id, "agent.teach_failed")
        await asyncio.sleep(0.1)
    assert holder["frame"][:2] == b"\xff\xd8"
    overview = (await user.get("/api/v1/training/overview?limit=200")).json()
    teach = [t for t in overview["trajectories"] if t["source"] == "teach"]
    labels = {t["chosen_action"].get("label") for t in teach}
    assert "Commence the process" in labels
    fills = {
        t["chosen_action"]["label"]: t["chosen_action"]["value_token"]
        for t in teach
        if t["chosen_action"]["action"] == "fill"
    }
    assert fills["Full name"] == "{{PROFILE.FULL_NAME}}"
    assert fills["Mobile number"] == "{{PROFILE.PHONE}}"
    body = json.dumps(overview)
    for leaked in (NAME, EMAIL, PHONE):
        assert leaked not in body

    # The remote browser is gone and the console was told.
    for _ in range(100):
        if not (await user.get("/api/v1/hitl/sessions")).json():
            break
        await asyncio.sleep(0.05)
    assert (await user.get("/api/v1/hitl/sessions")).json() == []
    closed = await _events(infra, user.id, "hitl.session_closed")
    assert closed and closed[-1]["id"] == session_id


@needs_browser
async def test_remote_portal_sign_in_resumes_the_application(
    rclient: httpx.AsyncClient,
    infra: Infrastructure,
    fake_portal: FakePortalServer,
    clean_db: DatabaseUrls,
) -> None:
    fake_portal.state.reset()
    user = await _student(rclient, CV_WITH_SKILLS)
    app_id = await _queued_application(infra, user, fake_portal.base + "/apply", "Sign-in Prog")
    conn = await asyncpg.connect(clean_db.owner)
    try:
        await conn.execute(
            "UPDATE applications SET state = 'BLOCKED_NEEDS_HUMAN', "
            "last_error = '[LOGIN] sign in to continue' WHERE id = $1",
            app_id,
        )
    finally:
        await conn.close()
    res = await user.post(f"/api/v1/applications/{app_id}/unblock")
    assert res.status_code == 202, res.text
    session = await _session_of(infra, user.id)
    assert session.kind == "PORTAL" and session.application_id == app_id
    assert (await _first_frame(infra, user.id, session.id))[:2] == b"\xff\xd8"

    # The portal page loaded in the stream (the first frame can be the blank tab) and the
    # capturer snapshotted its cookies (polls every 2 s); the user then pressed "Done".
    for _ in range(200):
        if fake_portal.state.hits["GET /apply"]:
            break
        await asyncio.sleep(0.05)
    assert fake_portal.state.hits["GET /apply"]
    await asyncio.sleep(2.5)
    closed = await user.post(f"/api/v1/hitl/sessions/{session.id}/close")
    assert closed.status_code == 204, closed.text
    for _ in range(300):
        if await _events(infra, user.id, "session.captured"):
            break
        assert not await _events(infra, user.id, "session.failed")
        await asyncio.sleep(0.1)
    for _ in range(300):  # the saved session resumes the application
        app = (await user.get(f"/api/v1/applications/{app_id}")).json()["application"]
        if app["state"] != "BLOCKED_NEEDS_HUMAN":
            break
        await asyncio.sleep(0.1)
    done = await _settled(user, app_id)
    assert done["application"]["state"] == "AWAITING_FINAL_VERIFICATION", done["transitions"]
    assert (await user.get("/api/v1/hitl/sessions")).json() == []


# -- WebSocket ----------------------------------------------------------------------------------


class _WsClient:
    """Minimal ASGI WebSocket client (httpx has no WebSocket transport)."""

    def __init__(self, app: Any, path: str, headers: list[tuple[bytes, bytes]]) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver"), *headers],
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
            "subprotocols": [],
        }
        self.inbox.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(app(scope, self.inbox.get, self.sent.put))

    async def next(self, timeout: float = 10) -> dict[str, Any]:
        return await asyncio.wait_for(self.sent.get(), timeout)

    def send_text(self, text: str) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": text})

    async def close(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        with __import__("contextlib").suppress(BaseException):
            await asyncio.wait_for(self.task, 10)


def _cookie(user: ApiUser) -> tuple[bytes, bytes]:
    token = user.client.cookies.get(ACCESS_COOKIE)
    assert token
    return (b"cookie", f"{ACCESS_COOKIE}={token}".encode())


async def test_websocket_stream_is_authenticated_origin_checked_and_tenant_isolated(
    rclient: httpx.AsyncClient, infra: Infrastructure, holder: dict[str, Any]
) -> None:
    hub = infra.hitl_hub
    assert hub is not None
    app = holder["app"]
    alice = await register(rclient, f"alice-{uuid.uuid4().hex[:6]}@example.com")
    bob = await register(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver"),
        f"bob-{uuid.uuid4().hex[:6]}@example.com",
    )
    session = HitlSession(uuid.uuid4(), alice.id, TEACH, "Portal", "https://p.edu")
    await hub.open(session)
    await hub.publish_frame(session.id, b"\xff\xd8first")
    path = f"/api/v1/hitl/sessions/{session.id}/stream"
    console = (b"origin", b"http://localhost:3000")

    evil = _WsClient(app, path, [(b"origin", b"https://evil.example"), _cookie(alice)])
    assert (await evil.next()) == {"type": "websocket.close", "code": 4403, "reason": ""}
    anonymous = _WsClient(app, path, [console])
    assert (await anonymous.next())["code"] == 4401
    other = _WsClient(app, path, [console, _cookie(bob)])
    assert (await other.next())["code"] == 4404  # never another tenant's browser

    ws = _WsClient(app, path, [console, _cookie(alice)])
    assert (await ws.next())["type"] == "websocket.accept"
    meta = json.loads((await ws.next())["text"])
    assert meta["type"] == "meta" and meta["viewport"] == {"width": 1280, "height": 900}
    assert (await ws.next())["bytes"] == b"\xff\xd8first"  # the latest frame at once
    async with hub.inputs(session.id) as inputs:
        ws.send_text('{"type": "mouse", "action": "click", "x": 99999, "y": 1}')  # dropped
        ws.send_text('{"type": "eval", "js": "alert(1)"}')  # dropped
        ws.send_text('{"type": "key", "action": "down", "key": "Enter"}')
        assert await inputs.get(timeout=5) == {"type": "key", "action": "down", "key": "Enter"}
        await hub.publish_frame(session.id, b"\xff\xd8second")
        assert (await ws.next())["bytes"] == b"\xff\xd8second"
    await hub.close(alice.id, session.id)
    assert json.loads((await ws.next())["text"]) == {"type": "closed"}
    await ws.close()
