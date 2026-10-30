"""Milestone 3 integration: crawler + extractor against a local HTTP portal, full campaign runs
through the API (dedupe, eligibility, deadline-ordered queue), SSE phase/counter streaming."""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import uvicorn

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_relay
from app.domain.discovery.models import CampaignSettings
from app.infrastructure.events.outbox_relay import OutboxRelayLoop
from app.infrastructure.scraping.fetchers import (
    PlaywrightFetcher,
    SafeHttpFetcher,
    playwright_available,
)
from app.infrastructure.scraping.site_crawler import SiteCrawler
from app.infrastructure.scraping.ssrf_guard import SsrfGuard
from tests.conftest import ApiUser, DatabaseUrls, register
from tests.fakes import FakeLLMFactory
from tests.portal import HIDDEN_INJECTION, Portal

CHAT_MODEL = "user/chat-model-x"  # whatever the user typed; must be used verbatim

PROFILE = {
    "full_name": "Ayesha Khan",
    "pii": {"phone": "+92 300 5550199"},
    "degree_level": "BACHELOR",
    "major": "Computer Science",
    "gpa": 3.62,
    "gpa_scale": 4,
    "nationality": "Pakistan",
    "country_of_residence": "Pakistan",
    "graduation_year": 2027,
    "experiences": [{"title": "Robotics team lead", "organization": "LUMS Robotics"}],
}


@pytest.fixture
async def fake_llm(infra: Infrastructure) -> AsyncIterator[FakeLLMFactory]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]
    yield fake
    infra.llm_factory = real
    await fake.aclose()


@pytest.fixture
async def dclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    runner = infra.campaign_runner
    if runner is not None:
        await runner.aclose()  # type: ignore[attr-defined]
        infra.campaign_runner = None


async def _ready_user(client: httpx.AsyncClient, email: str = "ayesha@example.com") -> ApiUser:
    user = await register(client, email)
    r = await user.client.put("/api/v1/profile", json=PROFILE, headers={"x-csrf-token": user.csrf})
    assert r.status_code == 200, r.text
    r = await user.post(
        "/api/v1/llm/providers",
        json={"kind": "NVIDIA_NIM", "model": CHAT_MODEL, "api_key": "nvapi-test-key-000000000000"},
    )
    assert r.status_code == 201, r.text
    return user


async def _wait_done(user: ApiUser, campaign_id: str, timeout: float = 90) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        detail: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{campaign_id}")).json()
        if detail["campaign"]["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return detail
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"campaign did not finish: {detail['campaign']}")
        await asyncio.sleep(0.2)


# ---- crawler ------------------------------------------------------------------------------------


async def test_crawler_follows_relevant_links_respects_robots_and_blocks_ssrf(
    portal: Portal,
) -> None:
    guard = SsrfGuard(allow_http=True, test_fixture_hosts=[portal.host])
    http = SafeHttpFetcher(guard)
    crawler = SiteCrawler(page_fetcher=http, http=http, guard=guard, politeness_seconds=0)
    before = len(portal.hits)
    pages = [
        p
        async for p in crawler.crawl(
            portal.base + "/",
            ["undergraduate scholarship"],
            CampaignSettings(max_pages_per_site=25, max_depth=3, auto_stage=False),
        )
    ]
    await crawler.aclose()
    urls = {p.final_url.removeprefix(portal.base) for p in pages if not p.blocked}
    hits = portal.hits[before:]
    assert {"/", "/scholarships?page=1", "/scholarships?page=2"} <= urls  # pagination followed
    assert {f"/opp/{s}" for s in ("alpha", "beta", "gamma", "delta", "alpha-copy")} <= urls
    assert not any(h.startswith("/private/") for h in hits)  # robots.txt honoured
    assert "/about" not in urls and not any("logo" in h for h in hits)  # irrelevant / assets
    blocked = [p for p in pages if p.blocked]
    assert len(blocked) == 1 and "169.254.169.254" in blocked[0].final_url  # redirect re-validated
    assert all("evil.example" not in p.final_url for p in pages)  # never leaves the site


@pytest.mark.skipif(not playwright_available(), reason="Chromium for Playwright is not installed")
async def test_playwright_route_guard_blocks_internal_subresources_and_redirects(
    portal: Portal,
) -> None:
    guard = SsrfGuard(allow_http=True, test_fixture_hosts=[portal.host])
    fetcher = PlaywrightFetcher(guard)
    try:
        page = await fetcher.fetch(portal.base + "/bad-assets")
        assert not page.blocked and page.status == 200
        assert "Scholarship assets page" in page.html
        assert page.blocked_requests >= 2  # metadata image + internal script were aborted
        redirected = await fetcher.fetch(portal.base + "/redirect-to-metadata")
        assert redirected.blocked and "169.254.169.254" in (redirected.error or "")
        direct = await fetcher.fetch("http://169.254.169.254/latest/meta-data/")
        assert direct.blocked
    finally:
        await fetcher.aclose()


# ---- campaigns ----------------------------------------------------------------------------------


async def test_full_campaign_dedupes_evaluates_and_queues_by_deadline(
    dclient: httpx.AsyncClient, portal: Portal, fake_llm: FakeLLMFactory, clean_db: DatabaseUrls
) -> None:
    user = await _ready_user(dclient)
    created = await user.post(
        "/api/v1/campaigns",
        json={
            "auto_stage": False,
            "name": "Fixture run",
            "scope": "BOTH",
            "keywords": ["undergraduate scholarship"],
            "target_urls": [portal.base + "/"],
            "use_search": False,
            "use_linkedin": False,
            "max_pages_per_site": 25,
            "max_depth": 3,
        },
    )
    assert created.status_code == 201, created.text
    detail = await _wait_done(user, created.json()["id"])
    campaign = detail["campaign"]
    assert campaign["status"] == "COMPLETED", campaign
    c = campaign["counters"]
    assert c["websites_crawled"] == 8  # index, 2 listing pages, 5 detail pages
    assert c["pages_blocked"] == 1  # redirect to cloud metadata
    assert c["opportunities_found"] == 4 and c["duplicates"] == 1  # alpha-copy deduped
    assert (c["eligible"], c["expired"], c["ineligible"], c["queued"]) == (2, 1, 1, 2)

    pages = {p["final_url"].removeprefix(portal.base): p for p in detail["pages"]}
    assert pages["/"]["injection_flags"][0]["pattern"] == "hidden_instruction"
    assert any(p["status"] == "BLOCKED_SSRF" for p in detail["pages"])

    # Only the user's exact model was used, and it never saw hidden or scripted content.
    assert fake_llm.models_used == {CHAT_MODEL}
    seen = "\n".join(fake_llm.documents_seen)
    assert HIDDEN_INJECTION not in seen and "window.secret" not in seen
    assert "<untrusted_document" in seen
    assert "+92 300" not in seen  # PII never sent to the LLM

    opps = {o["title"]: o for o in (await user.get("/api/v1/opportunities")).json()}
    assert {o["latest_outcome"] for o in opps.values()} == {"ELIGIBLE", "EXPIRED", "INELIGIBLE"}
    assert opps["Beta Research Internship 2020"]["latest_outcome"] == "EXPIRED"
    assert opps["Gamma Masters Fellowship"]["latest_outcome"] == "INELIGIBLE"
    assert (
        opps["Alpha Global Undergraduate Scholarship"]["portal_url"] == portal.base + "/apply/alpha"
    )
    eligible = (await user.get("/api/v1/opportunities", params={"outcome": "ELIGIBLE"})).json()
    assert [o["title"] for o in eligible] == [  # deadline urgency
        "Delta South Asia Merit Award",
        "Alpha Global Undergraduate Scholarship",
    ]

    alpha = (
        await user.get(
            f"/api/v1/opportunities/{opps['Alpha Global Undergraduate Scholarship']['id']}"
        )
    ).json()
    reasons = {r["rule"]: r for r in alpha["verdict"]["reasons"]}
    assert reasons["gpa"]["status"] == "PASS" and reasons["deadline"]["status"] == "PASS"
    assert reasons["Demonstrated leadership experience"]["source"] == "LLM"
    assert alpha["verdict"]["model"] == CHAT_MODEL
    gamma = (
        await user.get(f"/api/v1/opportunities/{opps['Gamma Masters Fellowship']['id']}")
    ).json()
    assert gamma["verdict"]["model"] is None  # decided in Stage 1: no LLM tokens spent

    queue = (await user.get("/api/v1/applications/queue")).json()
    assert [(q["priority_rank"], q["opportunity"]["title"], q["state"]) for q in queue] == [
        (1, "Delta South Asia Merit Award", "QUEUED"),
        (2, "Alpha Global Undergraduate Scholarship", "QUEUED"),
    ]
    summary = (await user.get("/api/v1/pipeline/summary")).json()
    assert (
        summary["websites_crawled"],
        summary["opportunities_found"],
        summary["eligible"],
        summary["queued"],
    ) == (8, 4, 2, 2)
    assert summary["latest_campaign"]["phase"] == "COMPLETED"

    conn = await asyncpg.connect(clean_db.owner)
    try:
        transitions = await conn.fetch(
            "SELECT from_state::text, to_state::text FROM application_transitions ORDER BY at"
        )
        phases = await conn.fetch(
            "SELECT payload->>'phase' AS phase FROM pipeline_events "
            "WHERE type = 'campaign.phase' ORDER BY seq"
        )
        order = await conn.fetch(
            "SELECT type, payload->>'phase' AS phase FROM pipeline_events "
            "WHERE type IN ('campaign.phase', 'eligibility.verdict') ORDER BY seq"
        )
    finally:
        await conn.close()
    assert [tuple(t) for t in transitions] == [("ELIGIBLE", "QUEUED")] * 2
    # Streaming pipeline: extraction + eligibility run WHILE crawling; EXTRACTING only drains.
    assert [p["phase"] for p in phases] == ["CRAWLING", "EXTRACTING", "COMPLETED"]
    first_verdict = next(i for i, r in enumerate(order) if r["type"] == "eligibility.verdict")
    drain = next(i for i, r in enumerate(order) if r["phase"] == "EXTRACTING")
    assert first_verdict < drain  # a verdict streamed before crawling finished

    # Re-running finds the same listings again: everything dedupes, nothing is re-queued.
    again = await user.post(
        "/api/v1/campaigns",
        json={
            "auto_stage": False,
            "keywords": ["undergraduate scholarship"],
            "target_urls": [portal.base + "/"],
            "use_search": False,
            "max_depth": 3,
            "max_pages_per_site": 25,
        },
    )
    second = (await _wait_done(user, again.json()["id"]))["campaign"]["counters"]
    assert second["opportunities_found"] == 0 and second["duplicates"] == 5
    assert len((await user.get("/api/v1/applications/queue")).json()) == 2


async def test_campaign_validation(dclient: httpx.AsyncClient, portal: Portal) -> None:
    user = await register(dclient, "novalid@example.com")
    no_llm = await user.post(
        "/api/v1/campaigns",
        json={"auto_stage": False, "keywords": ["x"], "target_urls": [portal.base + "/"]},
    )
    assert no_llm.status_code == 422 and "LLM" in no_llm.json()["error"]["message"]
    await user.post(
        "/api/v1/llm/providers",
        json={"kind": "NVIDIA_NIM", "model": CHAT_MODEL, "api_key": "nvapi-test-key-000000000000"},
    )
    unsafe = await user.post(
        "/api/v1/campaigns",
        json={
            "auto_stage": False,
            "keywords": ["x"],
            "target_urls": ["http://169.254.169.254/latest"],
            "use_search": False,
        },
    )
    assert unsafe.status_code == 422
    nothing = await user.post(
        "/api/v1/campaigns",
        json={
            "auto_stage": False,
            "keywords": ["x"],
            "use_websites": False,
            "use_search": False,
            "use_linkedin": False,
        },
    )
    assert nothing.status_code == 422


async def test_discovery_is_tenant_isolated(
    dclient: httpx.AsyncClient, infra: Infrastructure, fake_llm: FakeLLMFactory, portal: Portal
) -> None:
    alice = await _ready_user(dclient, "alice@example.com")
    created = await alice.post(
        "/api/v1/campaigns",
        json={
            "auto_stage": False,
            "keywords": ["scholarship"],
            "target_urls": [portal.base + "/"],
            "use_search": False,
            "max_depth": 3,
            "max_pages_per_site": 25,
        },
    )
    await _wait_done(alice, created.json()["id"])
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        bob = await register(other, "bob@example.com")
        assert (await bob.get("/api/v1/campaigns")).json() == []
        assert (await bob.get("/api/v1/opportunities")).json() == []
        assert (await bob.get("/api/v1/applications/queue")).json() == []
        assert (await bob.get(f"/api/v1/campaigns/{created.json()['id']}")).status_code == 404
        assert (
            await bob.post(f"/api/v1/campaigns/{created.json()['id']}/cancel")
        ).status_code == 404


# ---- live SSE ---------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_campaign_streams_phases_and_counters_over_sse(
    infra: Infrastructure, fake_llm: FakeLLMFactory, portal: Portal
) -> None:
    port = _free_port()
    app = create_app_with(infra, run_relay=False)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, loop="none", log_config=None, lifespan="off"
        )
    )
    serve = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.02)
    relay = OutboxRelayLoop(
        build_relay(infra), database_url=infra.settings.database_url, poll_interval=0.2
    )
    relay.start()
    events: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
            user = await _ready_user(http, "sse@example.com")
            async with http.stream("GET", "/api/v1/events/stream") as stream:
                created = await user.post(
                    "/api/v1/campaigns",
                    json={
                        "auto_stage": False,
                        "keywords": ["undergraduate scholarship"],
                        "target_urls": [portal.base + "/"],
                        "use_search": False,
                        "max_depth": 3,
                        "max_pages_per_site": 25,
                    },
                )
                assert created.status_code == 201
                cid = created.json()["id"]

                async def consume() -> None:
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            if event.get("campaign_id") == cid:
                                events.append(event)
                                if (
                                    event["type"] == "campaign.phase"
                                    and event["payload"]["phase"] == "COMPLETED"
                                ):
                                    return

                await asyncio.wait_for(consume(), timeout=90)
    finally:
        await relay.stop()
        server.should_exit = True
        await asyncio.wait_for(serve, timeout=10)
        if infra.campaign_runner is not None:
            await infra.campaign_runner.aclose()  # type: ignore[attr-defined]
            infra.campaign_runner = None

    phases = [e["payload"]["phase"] for e in events if e["type"] == "campaign.phase"]
    assert phases == ["CRAWLING", "EXTRACTING", "COMPLETED"]
    types = [e["type"] for e in events]
    assert types.count("page.crawled") == 9  # 8 distilled + 1 blocked
    assert types.count("opportunity.found") == 4
    assert types.count("eligibility.verdict") == 4
    assert types.count("application.queued") == 2
    crawled = [
        e["payload"]["counters"]["websites_crawled"] for e in events if e["type"] == "page.crawled"
    ]
    assert crawled == sorted(crawled) and crawled[-1] == 8  # live counters only grow
    final = events[-1]["payload"]["counters"]
    assert (final["eligible"], final["queued"]) == (2, 2)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert uuid.UUID(cid)


@pytest.mark.skipif(not playwright_available(), reason="Chromium for Playwright is not installed")
async def test_playwright_backed_crawl_of_portal(portal: Portal) -> None:
    guard = SsrfGuard(allow_http=True, test_fixture_hosts=[portal.host])
    http = SafeHttpFetcher(guard)
    crawler = SiteCrawler(
        page_fetcher=PlaywrightFetcher(guard), http=http, guard=guard, politeness_seconds=0
    )
    pages = [
        p
        async for p in crawler.crawl(
            portal.base + "/",
            ["scholarship"],
            CampaignSettings(max_pages_per_site=25, max_depth=3, auto_stage=False),
        )
    ]
    await crawler.aclose()
    rendered = {
        p.final_url.removeprefix(portal.base) for p in pages if not p.blocked and p.status == 200
    }
    assert {"/opp/alpha", "/opp/delta", "/scholarships?page=2"} <= rendered
    assert sum(1 for p in pages if p.blocked) == 1
