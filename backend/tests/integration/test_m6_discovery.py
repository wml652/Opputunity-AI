"""Milestone 6 discovery: in-page RAG (+ optional user reranker) -> one extraction call for a
long page; PDF opportunity documents; optional model slots blank by default."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

import httpx
import pytest

from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_discovery_deps
from app.domain.ports.discovery import SearchHit
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from tests.conftest import ApiUser, register
from tests.fakes import FakeLLMFactory
from tests.portal import Portal

CHAT_MODEL = "user/chat-model-x"
RERANK_MODEL = "user/typed-rerank-model"  # a value the USER typed in the form


class FakeSearch:
    name = "fake-search"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        return [SearchHit(title=u, url=u, snippet="", provider=self.name) for u in self.urls]


class FakeReranker:
    def __init__(self, model: str, calls: list[int]) -> None:
        self._model = model
        self._calls = calls

    @property
    def model(self) -> str:
        return self._model

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        self._calls.append(len(passages))
        return [
            1.0 if ("Title:" in p or "Deadline" in p or "Eligibility" in p) else 0.02
            for p in passages
        ]


class FakeRerankFactory:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.models: list[str] = []

    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> FakeReranker:
        self.models.append(model)
        return FakeReranker(model, self.calls)


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
def rerank_factory(infra: Infrastructure) -> AsyncIterator[FakeRerankFactory]:
    fake = FakeRerankFactory()
    infra.rerank_factory = fake
    yield fake  # type: ignore[misc]
    infra.rerank_factory = None


@pytest.fixture
async def m6client(
    infra: Infrastructure,
    fake_llm: FakeLLMFactory,
    search_urls: list[str],
    rerank_factory: FakeRerankFactory,
) -> AsyncIterator[httpx.AsyncClient]:
    deps = replace(build_discovery_deps(infra), search=FakeSearch(search_urls), search_factory=None)
    infra.campaign_runner = InProcessCampaignRunner(RunCampaign(deps))
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await infra.campaign_runner.aclose()  # type: ignore[union-attr]
    infra.campaign_runner = None


async def _user(client: httpx.AsyncClient, **provider: Any) -> ApiUser:
    user = await register(client, f"m6-{uuid.uuid4().hex[:8]}@example.com")
    r = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": CHAT_MODEL,
            "api_key": "nvapi-test-key-000000000000",
            **provider,
        },
    )
    assert r.status_code == 201, r.text
    return user


async def _campaign(user: ApiUser) -> dict[str, Any]:
    created = await user.post(
        "/api/v1/campaigns",
        json={
            "keywords": ["research fellowship"],
            "scope": "INTERNATIONAL",
            "use_websites": False,
            "max_search_queries": 1,
        },
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{cid}")).json()
        if detail["campaign"]["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return detail
        await asyncio.sleep(0.1)
    raise AssertionError("campaign did not finish")


def _extraction_calls(fake: FakeLLMFactory, marker: str) -> int:
    return sum(
        1
        for llm in fake.created
        for call in llm.calls
        if "extract structured opportunity" in call[0].content and marker in call[1].content
    )


async def test_long_page_rag_with_reranker_needs_one_llm_call(
    m6client: httpx.AsyncClient,
    portal: Portal,
    search_urls: list[str],
    fake_llm: FakeLLMFactory,
    rerank_factory: FakeRerankFactory,
) -> None:
    search_urls[:] = [portal.base + "/long-detail"]
    user = await _user(m6client, rerank_model=RERANK_MODEL)
    detail = await _campaign(user)
    assert detail["campaign"]["status"] == "COMPLETED"
    (page,) = detail["pages"]
    assert page["status"] == "EXTRACTED"
    trace = (
        await user.get(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}")
    ).json()["trace"]
    rag = next(s for s in trace if s["stage"] == "RAG_FILTER")["detail"]
    assert rag["chars_saved_pct"] > 80 and rag["single_window"] is True
    assert rag["rag_sections_kept"] < rag["rag_sections_total"]
    assert rag["scorer"].startswith(f"rerank:{RERANK_MODEL}")
    extraction = next(s for s in trace if s["stage"] == "LLM_EXTRACTION")["detail"]
    assert extraction["chunks"] == 1
    assert _extraction_calls(fake_llm, "Rho Global Research Fellowship") == 1  # was 3+ windows
    assert rerank_factory.models == [RERANK_MODEL] and rerank_factory.calls
    opps = (await user.get("/api/v1/opportunities")).json()
    assert [o["title"] for o in opps] == ["Rho Global Research Fellowship"]


async def test_without_a_reranker_the_local_hybrid_scorer_is_used(
    m6client: httpx.AsyncClient,
    portal: Portal,
    search_urls: list[str],
    fake_llm: FakeLLMFactory,
    rerank_factory: FakeRerankFactory,
) -> None:
    search_urls[:] = [portal.base + "/long-detail"]
    user = await _user(m6client)  # rerank_model left blank
    detail = await _campaign(user)
    (page,) = detail["pages"]
    trace = (
        await user.get(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}")
    ).json()["trace"]
    rag = next(s for s in trace if s["stage"] == "RAG_FILTER")["detail"]
    assert rag["scorer"].startswith("local hybrid") and rag["chars_saved_pct"] > 80
    assert rerank_factory.models == []  # never called without the user's model
    assert _extraction_calls(fake_llm, "Rho Global Research Fellowship") == 1


async def test_pdf_opportunity_document_is_parsed_and_extracted(
    m6client: httpx.AsyncClient, portal: Portal, search_urls: list[str]
) -> None:
    search_urls[:] = [portal.base + "/call-for-applications.pdf"]
    user = await _user(m6client)
    detail = await _campaign(user)
    (page,) = detail["pages"]
    assert page["status"] == "EXTRACTED", page
    trace = (
        await user.get(f"/api/v1/campaigns/{detail['campaign']['id']}/pages/{page['id']}")
    ).json()["trace"]
    parsed = next(s for s in trace if s["stage"] == "PDF_PARSE")["detail"]
    assert parsed["method"] == "pypdf" and parsed["pages"] == 1
    titles = [o["title"] for o in (await user.get("/api/v1/opportunities")).json()]
    assert titles == ["Sigma Summer Research Programme"]


async def test_optional_model_slots_are_blank_by_default(m6client: httpx.AsyncClient) -> None:
    user = await _user(m6client)
    (provider,) = (await user.get("/api/v1/llm/providers")).json()
    assert provider["rerank_model"] is None and provider["vision_model"] is None
    assert provider["extraction_model"] is None and provider["rpm_limit"] == 36
    updated = await user.client.patch(
        f"/api/v1/llm/providers/{provider['id']}",
        json={"rpm_limit": 20, "vision_model": "user/typed-vision-model"},
        headers={"x-csrf-token": user.csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rpm_limit"] == 20
    assert updated.json()["vision_model"] == "user/typed-vision-model"
