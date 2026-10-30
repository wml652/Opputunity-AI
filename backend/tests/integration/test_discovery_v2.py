"""Discovery v2: bounded proxy shutdown, hub -> detail harvesting, streaming pipeline, bot
challenges (+ user-solved sessions), failed pages never dropped, no silent extraction caps,
per-window isolation, fast cancellation, one active campaign, optional extraction model, per-user
search keys, deadlock-free playbook saves."""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import asyncpg
import httpx
import pytest

import tests.fakes as fakes
import tests.portal as portal_module
from app.application.discovery.runner import CampaignRun
from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_discovery_deps
from app.domain.discovery.models import Campaign, CampaignPhase, CampaignSettings
from app.domain.ports.discovery import SearchHit
from app.domain.shared.enums import CampaignStatus, Scope
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from app.infrastructure.scraping.egress_proxy import EgressProxy
from app.infrastructure.scraping.ssrf_guard import SsrfGuard
from tests.conftest import ApiUser, DatabaseUrls, register
from tests.fakes import FakeLLMFactory
from tests.fixtures.fake_portal.app import FakePortalServer
from tests.portal import Portal

CHAT_MODEL = "user/chat-model-x"
FAST_MODEL = "user/fast-extractor"


class FakeSearch:
    name = "fake-search"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        return [SearchHit(title=u, url=u, snippet="", provider=self.name) for u in self.urls]


class FakeCapturer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def capture(self, url: str, *, timeout_s: float) -> str:
        self.urls.append(url)
        return json.dumps({"cookies": [{"name": "cf_clearance", "value": "ok"}], "origins": []})


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
def capturer(infra: Infrastructure) -> FakeCapturer:
    fake = FakeCapturer()
    infra.session_capturer = fake
    return fake


@pytest.fixture
async def vclient(
    infra: Infrastructure,
    fake_llm: FakeLLMFactory,
    search_urls: list[str],
    capturer: FakeCapturer,
    fake_portal: FakePortalServer,
) -> AsyncIterator[httpx.AsyncClient]:
    portal_module.OFFSITE_BASE = fake_portal.base
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls), search_factory=None)
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None
    infra.session_capturer = None
    portal_module.OFFSITE_BASE = ""


async def _user(client: httpx.AsyncClient, *, extraction_model: str | None = None) -> ApiUser:
    user = await register(client, f"v2-{uuid.uuid4().hex[:8]}@example.com")
    await user.client.put(
        "/api/v1/profile",
        json={
            "full_name": "Ayesha Khan",
            "degree_level": "BACHELOR",
            "gpa": 3.6,
            "gpa_scale": 4,
            "nationality": "Pakistan",
            "country_of_residence": "Pakistan",
            "graduation_year": 2027,
        },
        headers={"x-csrf-token": user.csrf},
    )
    body: dict[str, Any] = {
        "kind": "NVIDIA_NIM",
        "model": CHAT_MODEL,
        "api_key": "nvapi-test-key-000000000000",
    }
    if extraction_model:
        body["extraction_model"] = extraction_model
    r = await user.post("/api/v1/llm/providers", json=body)
    assert r.status_code == 201, r.text
    return user


async def _start(user: ApiUser, **body: Any) -> str:
    payload = {
        "keywords": ["research internship"],
        "scope": "INTERNATIONAL",
        "use_websites": False,
        "use_search": True,
        "max_search_queries": 1,
        **body,
    }
    created = await user.post("/api/v1/campaigns", json=payload)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _wait(user: ApiUser, cid: str, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{cid}")).json()
        if detail["campaign"]["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return detail
        await asyncio.sleep(0.1)
    raise AssertionError("campaign did not finish")


async def _page(user: ApiUser, cid: str, page_id: str) -> dict[str, Any]:
    out: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{cid}/pages/{page_id}")).json()
    return out


# ---- P0: bounded proxy shutdown -----------------------------------------------------------------


async def test_egress_proxy_stop_is_bounded_with_lingering_tunnels() -> None:
    held: list[asyncio.StreamWriter] = []

    async def hold(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        held.append(w)  # accept and never answer, never close (a keep-alive peer)
        await asyncio.sleep(3600)

    upstream = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    proxy = EgressProxy(SsrfGuard(allow_http=True, test_fixture_hosts=[f"127.0.0.1:{port}"]))
    proxy_port = await proxy.start()
    try:
        tunnel_r, tunnel_w = await asyncio.open_connection("127.0.0.1", proxy_port)
        tunnel_w.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        await tunnel_w.drain()
        assert b"200" in await asyncio.wait_for(tunnel_r.readline(), 5)
        _fwd_r, fwd_w = await asyncio.open_connection("127.0.0.1", proxy_port)
        fwd_w.write(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await fwd_w.drain()
        await asyncio.sleep(0.3)
        assert proxy.open_connections >= 3
        started = time.monotonic()
        await proxy.stop()
        assert time.monotonic() - started < 2.5  # used to hang forever (Server.wait_closed)
        assert proxy.open_connections == 0
        tunnel_w.close()
        fwd_w.close()
    finally:
        upstream.close()
        for w in held:
            w.close()


async def test_dns_failure_is_not_an_ssrf_block() -> None:
    from app.infrastructure.scraping.fetchers import SafeHttpFetcher

    async def no_dns(host: str, port: int) -> list[str]:
        raise OSError("getaddrinfo failed")

    fetcher = SafeHttpFetcher(SsrfGuard(allow_http=True, resolver=no_dns))
    try:
        page = await fetcher.fetch("https://does-not-resolve.example/x")
    finally:
        await fetcher.aclose()
    assert not page.blocked and (page.error or "").startswith("dns_error")


# ---- hub -> detail harvesting + streaming --------------------------------------------------------


async def test_hub_harvest_follows_details_offsite_and_skips_pagination(
    vclient: httpx.AsyncClient,
    portal: Portal,
    search_urls: list[str],
    clean_db: DatabaseUrls,
    fake_llm: FakeLLMFactory,
) -> None:
    search_urls[:] = [portal.base + "/hub"]
    user = await _user(vclient, extraction_model=FAST_MODEL)
    detail = await _wait(user, await _start(user))
    c = detail["campaign"]
    assert c["status"] == "COMPLETED", c
    pages = {p["url"]: p for p in detail["pages"]}
    hub = pages[portal.base + "/hub"]
    assert hub["status"] == "EXTRACTED"
    trace = (await _page(user, c["id"], hub["id"]))["trace"]
    harvest = next(s for s in trace if s["stage"] == "HUB_HARVEST")
    assert harvest["detail"]["cards"] == 9 and harvest["detail"]["queued"] == 9
    details = [p for p in detail["pages"] if p["discovery_source"] == "hub_detail"]
    assert len(details) == 9 and all(p["status"] == "EXTRACTED" for p in details)
    assert any("/official/eta-fellowship-2099" in p["url"] for p in details)  # off-site
    hits = portal.hits
    assert not any("/page/38" in h or "/tag/" in h for h in hits)  # pagination clutter skipped
    counters = c["counters"]
    assert counters["opportunities_found"] == 9
    assert counters["hubs_harvested"] == 1 and counters["details_queued"] == 9
    # The hub itself never went to the LLM; every extraction used the user's fast model.
    extraction_models = {
        llm.model
        for llm in fake_llm.created
        for call in llm.calls
        if "extract structured opportunity" in call[0].content
    }
    assert extraction_models == {FAST_MODEL}
    docs = [
        call[1].content
        for llm in fake_llm.created
        for call in llm.calls
        if "extract structured opportunity" in call[0].content
    ]
    assert not any("Latest research internships" in d for d in docs)
    conn = await asyncpg.connect(clean_db.owner)
    try:
        order = await conn.fetch(
            "SELECT type, payload->>'phase' AS phase FROM pipeline_events "
            "WHERE type IN ('campaign.phase', 'eligibility.verdict') ORDER BY seq"
        )
    finally:
        await conn.close()
    verdict = next(i for i, r in enumerate(order) if r["type"] == "eligibility.verdict")
    drain = next(i for i, r in enumerate(order) if r["phase"] == "EXTRACTING")
    assert verdict < drain  # verdicts streamed while crawling


async def test_no_silent_caps_and_window_isolation(
    vclient: httpx.AsyncClient, portal: Portal, search_urls: list[str]
) -> None:
    search_urls[:] = [portal.base + "/many", portal.base + "/windowed"]
    user = await _user(vclient)
    detail = await _wait(user, await _start(user))
    c = detail["campaign"]
    assert c["status"] == "COMPLETED"
    titles = {
        o["title"] for o in (await user.get(f"/api/v1/opportunities?campaign_id={c['id']}")).json()
    }
    assert sum(1 for t in titles if t.startswith("Omicron Grant")) == 30  # old cap: 10
    assert {"Pi Grant One", "Pi Grant Three"} <= titles  # the timed-out window lost nothing else
    windowed = next(p for p in detail["pages"] if p["url"].endswith("/windowed"))
    assert windowed["status"] == "EXTRACTED"
    trace = (await _page(user, c["id"], windowed["id"]))["trace"]
    extraction = next(s for s in trace if s["stage"] == "LLM_EXTRACTION")
    assert extraction["detail"]["failed_windows"] == 1
    assert any("timed out" in (a.get("error") or "") for a in extraction["detail"]["attempts"])


# ---- bot challenge + user-solved session --------------------------------------------------------


async def test_bot_challenge_is_remembered_and_solved_session_lifts_it(
    vclient: httpx.AsyncClient,
    portal: Portal,
    search_urls: list[str],
    capturer: FakeCapturer,
    clean_db: DatabaseUrls,
) -> None:
    search_urls[:] = [portal.base + "/challenge"]
    user = await _user(vclient)
    first = await _wait(user, await _start(user))
    (page,) = first["pages"]
    assert page["status"] == "BLOCKED_BOT_CHALLENGE" and page["error_code"] == "bot_challenge"
    assert first["campaign"]["counters"]["bot_challenges"] == 1
    assert first["campaign"]["counters"]["pages_retried"] == 0  # no wasted escalations

    second = await _wait(user, await _start(user))
    (skipped,) = second["pages"]
    assert skipped["status"] == "SKIPPED" and skipped["error_code"] == "bot_challenge"
    assert "Browser Sessions" in skipped["error"]

    listing = (await user.get("/api/v1/browser-sessions")).json()
    assert listing["headful_available"] is True
    (challenged,) = listing["challenged"]
    assert challenged["solved"] is False
    started = await user.post(
        "/api/v1/browser-sessions/capture", json={"url": portal.base + "/challenge"}
    )
    assert started.status_code == 202, started.text
    for _ in range(100):
        listing = (await user.get("/api/v1/browser-sessions")).json()
        if listing["sessions"]:
            break
        await asyncio.sleep(0.05)
    (session,) = listing["sessions"]
    assert session["kind"] == "CHALLENGE" and listing["challenged"] == []
    conn = await asyncpg.connect(clean_db.owner)
    try:
        raw = await conn.fetchval("SELECT storage_state_ciphertext FROM browser_sessions")
    finally:
        await conn.close()
    assert b"cf_clearance" not in bytes(raw)  # encrypted at rest

    third = await _wait(user, await _start(user))
    assert third["pages"][0]["status"] != "SKIPPED"  # the domain is crawled again

    linkedin = await user.post(
        "/api/v1/browser-sessions/capture", json={"kind": "LINKEDIN", "url": ""}
    )
    assert linkedin.status_code == 422 and "acknowledge" in linkedin.json()["error"]["message"]


# ---- failed pages are never dropped by content hash ---------------------------------------------


async def test_identical_failed_pages_are_all_kept(
    vclient: httpx.AsyncClient, portal: Portal, search_urls: list[str]
) -> None:
    search_urls[:] = [portal.base + "/empty-a", portal.base + "/empty-b"]
    user = await _user(vclient)
    detail = await _wait(user, await _start(user))
    failed = [p for p in detail["pages"] if p["status"] == "FAILED"]
    assert {p["url"].rsplit("/", 1)[-1] for p in failed} == {"empty-a", "empty-b"}
    assert detail["campaign"]["counters"]["pages_failed"] == 2


# ---- cancellation, one active campaign ---------------------------------------------------------


async def test_cancel_stops_within_two_seconds_and_one_active_campaign(
    vclient: httpx.AsyncClient,
    infra: Infrastructure,
    portal: Portal,
    search_urls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fakes, "SLOW_PAGE_SECONDS", 120.0)
    search_urls[:] = [portal.base + "/slow"]
    user = await _user(vclient)
    cid = await _start(user)
    for _ in range(300):  # wait until the slow page is in extraction
        pages = (await user.get(f"/api/v1/campaigns/{cid}")).json()["pages"]
        if pages:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    blocked = await user.post(
        "/api/v1/campaigns", json={"keywords": ["x"], "use_websites": False, "use_search": True}
    )
    assert blocked.status_code == 409 and "still running" in blocked.json()["error"]["message"]
    started = time.monotonic()
    assert (await user.post(f"/api/v1/campaigns/{cid}/cancel")).status_code == 204
    runner = infra.campaign_runner
    assert isinstance(runner, InProcessCampaignRunner)
    await runner.wait_idle(timeout=5)
    assert time.monotonic() - started < 2.0
    detail = (await user.get(f"/api/v1/campaigns/{cid}")).json()
    assert detail["campaign"]["status"] == "CANCELLED"


# ---- per-user search keys -----------------------------------------------------------------------


async def test_search_keys_are_encrypted_and_masked(
    vclient: httpx.AsyncClient, clean_db: DatabaseUrls
) -> None:
    user = await register(vclient, "searchkeys@example.com")
    saved = await user.client.put(
        "/api/v1/search/credentials/tavily",
        json={"api_key": "tvly-SECRET-abcdef123456"},
        headers={"x-csrf-token": user.csrf},
    )
    assert saved.status_code == 200 and saved.json()["key_last4"] == "3456"
    assert "SECRET" not in saved.text
    listed = (await user.get("/api/v1/search/credentials")).json()
    assert [k["provider"] for k in listed] == ["tavily"]
    conn = await asyncpg.connect(clean_db.owner)
    try:
        raw = await conn.fetchval("SELECT key_ciphertext FROM search_credentials")
    finally:
        await conn.close()
    assert b"SECRET" not in bytes(raw)
    bad = await user.client.put(
        "/api/v1/search/credentials/google",
        json={"api_key": "whatever-12345"},
        headers={"x-csrf-token": user.csrf},
    )
    assert bad.status_code == 422
    deleted = await user.client.delete(
        "/api/v1/search/credentials/tavily", headers={"x-csrf-token": user.csrf}
    )
    assert deleted.status_code == 204


# ---- concurrent playbook saves never deadlock ---------------------------------------------------


async def test_concurrent_playbook_saves_do_not_deadlock(
    vclient: httpx.AsyncClient, infra: Infrastructure
) -> None:
    user = await register(vclient, "deadlock@example.com")
    deps = build_discovery_deps(infra)
    domains = [f"site{i}.example.org" for i in range(12)]

    def run() -> CampaignRun:
        now = infra.clock.now()
        campaign = Campaign(
            id=uuid.uuid4(),
            user_id=user.id,
            name="x",
            scope=Scope.INTERNATIONAL,
            keywords=["x"],
            status=CampaignStatus.RUNNING,
            phase=CampaignPhase.CRAWLING,
            settings=CampaignSettings(),
            counters={},
            error=None,
            created_at=now,
            sources=[],
        )
        return CampaignRun(deps, campaign)

    async def hammer(r: CampaignRun) -> None:
        for _ in range(8):
            order = domains[:]
            random.shuffle(order)  # different touch order per run, like real crawls
            for d in order:
                pb = r._playbook(f"https://{d}/")
                pb.successes += 1
                r._touch(pb)
            await r.save_playbooks()

    await asyncio.gather(*(hammer(run()) for _ in range(4)))
