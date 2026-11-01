"""Part 1: flight-recorder traces (DB + disk), adaptive retries, cross-campaign failure memory."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

import tests.fakes as fakes
from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_discovery_deps
from app.domain.ports.discovery import SearchHit
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from tests.conftest import ApiUser, DatabaseUrls, register
from tests.fakes import FakeLLMFactory
from tests.portal import Portal

CHAT_MODEL = "user/chat-model-x"


class FakeSearch:
    name = "fake-search"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        return [SearchHit(title=u, url=u, snippet="", provider=self.name) for u in self.urls]


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
async def hclient(
    infra: Infrastructure, fake_llm: FakeLLMFactory, search_urls: list[str]
) -> AsyncIterator[httpx.AsyncClient]:
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls))
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None


async def _user(client: httpx.AsyncClient) -> ApiUser:
    user = await register(client, "heal@example.com")
    await user.client.put(
        "/api/v1/profile",
        json={
            "full_name": "Ayesha Khan",
            "degree_level": "BACHELOR",
            "gpa": 3.6,
            "gpa_scale": 4,
            "nationality": "Pakistan",
            "graduation_year": 2027,
        },
        headers={"x-csrf-token": user.csrf},
    )
    r = await user.post(
        "/api/v1/llm/providers",
        json={"kind": "NVIDIA_NIM", "model": CHAT_MODEL, "api_key": "nvapi-test-key-000000000000"},
    )
    assert r.status_code == 201
    return user


async def _run(user: ApiUser, **body: Any) -> dict[str, Any]:
    payload = {
        "keywords": ["scholarship"],
        "use_search": False,
        "max_depth": 0,
        "max_pages_per_site": 5,
        **body,
    }
    created = await user.post("/api/v1/campaigns", json={"auto_stage": False, **payload})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{cid}")).json()
        if detail["campaign"]["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return detail
        await asyncio.sleep(0.1)
    raise AssertionError("campaign did not finish")


async def _page_detail(user: ApiUser, detail: dict[str, Any], suffix: str) -> dict[str, Any]:
    page = next(p for p in detail["pages"] if p["url"].endswith(suffix))
    data: dict[str, Any] = (
        await user.get(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}")
    ).json()
    return data


async def test_long_page_is_rag_filtered_traced_and_logged(
    hclient: httpx.AsyncClient, portal: Portal, infra: Infrastructure
) -> None:
    user = await _user(hclient)
    detail = await _run(user, target_urls=[portal.base + "/long-blog"])
    assert detail["campaign"]["counters"]["opportunities_found"] == 1
    page = await _page_detail(user, detail, "/long-blog")
    assert page["status"] == "EXTRACTED" and page["markdown_chars"] > 14_000
    stages = [s["stage"] for s in page["trace"]]
    assert stages[:4] == ["DISCOVERY", "PREFLIGHT_AND_SSRF", "HTTP_FETCH", "DISTILL_AND_SANITIZE"]
    # M6: the in-page RAG filter reduces the 14k-char page to one window (was >= 3 chunks).
    rag = next(s for s in page["trace"] if s["stage"] == "RAG_FILTER")
    assert rag["detail"]["chars_saved_pct"] > 50 and rag["detail"]["single_window"] is True
    extraction = next(s for s in page["trace"] if s["stage"] == "LLM_EXTRACTION")
    assert extraction["status"] == "ok" and extraction["detail"]["chunks"] == 1
    assert extraction["detail"]["model"] == CHAT_MODEL
    assert extraction["detail"]["opportunities"] == 1
    fetch = next(s for s in page["trace"] if s["stage"] == "HTTP_FETCH")
    assert fetch["detail"]["http_status"] == 200 and fetch["detail"]["html_bytes"] > 14_000
    assert page["discovery_source"] == "seed" and page["markdown_preview"]

    log_dir = Path(infra.settings.logs_dir) / "campaigns"
    jsonl = log_dir / f"campaign_{detail['campaign']['id']}.jsonl"
    human = log_dir / f"campaign_{detail['campaign']['id']}.log"
    kinds = [json.loads(line)["kind"] for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert {"start", "memory_loaded", "page", "page_extraction", "opportunity", "phase"} <= set(
        kinds
    )
    text = human.read_text(encoding="utf-8")
    assert "LLM_EXTRACTION" in text and "/long-blog" in text and page["log_file"] == str(human)


async def test_extraction_failure_adapts_this_campaign_and_the_next(
    hclient: httpx.AsyncClient,
    portal: Portal,
    monkeypatch: pytest.MonkeyPatch,
    clean_db: DatabaseUrls,
) -> None:
    monkeypatch.setattr(fakes, "TRUNCATE_ABOVE_CHARS", 4_000)  # 6k windows now get cut off
    user = await _user(hclient)
    first = await _run(user, target_urls=[portal.base + "/long-blog"])
    page = await _page_detail(user, first, "/long-blog")
    attempts = [s for s in page["trace"] if s["stage"] == "LLM_EXTRACTION"]
    assert [a["status"] for a in attempts] == ["fail", "ok"]  # escalated within the run
    assert attempts[0]["detail"]["attempts"][0]["finish_reason"] == "length"
    assert attempts[1]["detail"]["chunk_chars"] == 3_000
    assert page["status"] == "EXTRACTED"

    conn = await asyncpg.connect(clean_db.owner)
    try:
        row = await conn.fetchrow("SELECT extraction_strategy, failures FROM domain_playbooks")
    finally:
        await conn.close()
    assert json.loads(row["extraction_strategy"])["chunk_chars"] == 3_000
    assert json.loads(row["failures"])["LLM_EXTRACTION:invalid_json"] >= 1

    # Next campaign starts with the learned strategy: one attempt, no failure.
    second = await _run(user, target_urls=[portal.base + "/long-blog?again=1"])
    page2 = await _page_detail(user, second, "/long-blog?again=1")
    attempts2 = [s for s in page2["trace"] if s["stage"] == "LLM_EXTRACTION"]
    assert len(attempts2) == 1 and attempts2[0]["status"] == "ok"
    assert attempts2[0]["detail"]["chunk_chars"] == 3_000
    events = (await user.get("/api/v1/events", params={"limit": 500})).json()
    assert any(
        e["type"] == "page.adapted" and "failure memory" in e["payload"]["message"] for e in events
    )


async def test_403_is_retried_adaptively_and_remembered(
    hclient: httpx.AsyncClient, portal: Portal, clean_db: DatabaseUrls
) -> None:
    user = await _user(hclient)
    detail = await _run(user, target_urls=[portal.base + "/blocked-once/t1"])
    page = await _page_detail(user, detail, "/blocked-once/t1")
    assert page["status"] == "EXTRACTED"
    retry = next(s for s in page["trace"] if s["stage"] == "MEMORY")
    assert retry["detail"]["action"] == "adaptive_retry" and retry["detail"]["reason"] == "http_403"
    statuses = [s["detail"].get("http_status") for s in page["trace"] if s["stage"] == "HTTP_FETCH"]
    assert statuses == [403, 200]
    assert detail["campaign"]["counters"]["pages_retried"] >= 1
    conn = await asyncpg.connect(clean_db.owner)
    try:
        strategy = json.loads(await conn.fetchval("SELECT fetch_strategy FROM domain_playbooks"))
    finally:
        await conn.close()
    assert strategy["level"] >= 1  # the working strategy is remembered for the domain

    second = await _run(user, target_urls=[portal.base + "/blocked-once/t2"])
    events = (await user.get("/api/v1/events", params={"limit": 500})).json()
    adapted = [
        e
        for e in events
        if e["type"] == "page.adapted" and e["campaign_id"] == second["campaign"]["id"]
    ]
    assert adapted and "Adapted fetch strategy" in adapted[0]["payload"]["message"]


async def test_dead_search_result_is_skipped_by_failure_memory(
    hclient: httpx.AsyncClient, portal: Portal, search_urls: list[str]
) -> None:
    search_urls.extend([portal.base + "/gone", portal.base + "/long-blog"])
    user = await _user(hclient)
    first = await _run(user, use_websites=False, use_search=True)
    gone = await _page_detail(user, first, "/gone")
    assert gone["status"] == "FAILED" and gone["error_code"] == "http_404"
    assert gone["discovery_source"] == "web_search" and gone["discovery_query"]

    second = await _run(user, use_websites=False, use_search=True)
    skipped = await _page_detail(user, second, "/gone")
    assert skipped["status"] == "SKIPPED"
    short = first["campaign"]["id"][-6:]
    assert f"Skipped based on Campaign #{short} failure memory" in skipped["error"]
    assert second["campaign"]["counters"]["pages_skipped"] == 1
    events = (await user.get("/api/v1/events", params={"limit": 500})).json()
    assert any(
        e["type"] == "page.skipped" and "failure memory" in e["payload"]["message"] for e in events
    )


async def test_retry_page_with_adaptive_strategy(
    hclient: httpx.AsyncClient, portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fakes, "TRUNCATE_ABOVE_CHARS", 100)  # every attempt truncates
    user = await _user(hclient)
    detail = await _run(user, target_urls=[portal.base + "/long-blog"])
    page = await _page_detail(user, detail, "/long-blog")
    assert page["status"] == "FAILED" and page["error_stage"] == "LLM_EXTRACTION"
    assert page["error_code"] == "llm_truncated"

    monkeypatch.setattr(fakes, "TRUNCATE_ABOVE_CHARS", 6_500)
    r = await user.post(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}/retry")
    assert r.status_code == 202
    for _ in range(300):
        again = (
            await user.get(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}")
        ).json()
        if again["status"] == "EXTRACTED":
            break
        await asyncio.sleep(0.1)
    assert again["status"] == "EXTRACTED" and again["error_code"] is None
    assert any(
        s["detail"].get("action") == "manual_retry"
        for s in again["trace"]
        if s["stage"] == "MEMORY"
    )
    titles = [o["title"] for o in (await user.get("/api/v1/opportunities")).json()]
    assert "Omega Long-Read Fellowship" in titles
