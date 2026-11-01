"""Track 0 (stabilize): provider fail-fast, honest completion, long titles, playbook saves and
readiness - the failures found by the 2026-11-01 log audit."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
import httpx
import pytest

from app.bootstrap.container import Infrastructure
from app.domain.discovery.playbook import DomainPlaybook
from app.domain.llm.provider import ConnectionErrorCode
from app.infrastructure.config.settings import Settings
from tests.conftest import KEY_V1, DatabaseUrls
from tests.fakes import FakeLLMFactory
from tests.integration import test_self_healing as heal
from tests.portal import HUB_DEALS, Portal

# Shared fixtures: a fake model factory and a client whose campaign runner uses fake search.
fake_llm = heal.fake_llm
hclient = heal.hclient
search_urls = heal.search_urls
_run, _user = heal._run, heal._user


def _deals(portal: Portal) -> list[str]:
    return [f"{portal.base}/deal/{slug}" for slug in HUB_DEALS]


async def test_rejected_key_fails_the_campaign_fast_without_poisoning_playbooks(
    hclient: httpx.AsyncClient,
    portal: Portal,
    fake_llm: FakeLLMFactory,
    clean_db: DatabaseUrls,
) -> None:
    user = await _user(hclient)
    fake_llm.reject = (ConnectionErrorCode.FORBIDDEN, 403)
    detail = await _run(user, target_urls=_deals(portal))
    campaign = detail["campaign"]
    assert campaign["status"] == "FAILED", campaign
    assert campaign["error"].startswith("Model provider rejected the request (HTTP 403")
    # 3 rejections trip the breaker; at most one in-flight call per extraction worker follows.
    # (Before: every page x every escalated strategy x every window - 337 calls on 2026-11-01.)
    assert fake_llm.call_count <= 3 + 4, fake_llm.call_count
    conn = await asyncpg.connect(clean_db.owner)
    try:
        rows = await conn.fetch("SELECT failures, extraction_strategy FROM domain_playbooks")
    finally:
        await conn.close()
    for row in rows:  # a provider outage is not a lesson about the site
        assert not any(k.startswith("LLM_EXTRACTION") for k in json.loads(row["failures"]))
        assert json.loads(row["extraction_strategy"]).get("level", 0) == 0


async def test_rate_limits_do_not_trip_the_breaker_but_completion_is_honest(
    hclient: httpx.AsyncClient, portal: Portal, fake_llm: FakeLLMFactory
) -> None:
    user = await _user(hclient)
    fake_llm.reject = (ConnectionErrorCode.RATE_LIMITED, 429)
    detail = await _run(user, target_urls=_deals(portal)[:3])
    campaign = detail["campaign"]
    assert campaign["status"] == "COMPLETED" and campaign["error"] is None
    progress = campaign["progress"]
    assert progress["outcome"] == "failed"
    assert progress["outcome_detail"] == "3 of 3 pages failed extraction"
    assert progress["current"]["label"] == "Completed - nothing extracted"


async def test_healthy_campaign_completes_ok_and_long_titles_fit(
    hclient: httpx.AsyncClient, portal: Portal
) -> None:
    user = await _user(hclient)
    detail = await _run(user, target_urls=[portal.base + "/long-title", _deals(portal)[0]])
    campaign = detail["campaign"]
    assert campaign["status"] == "COMPLETED", campaign
    assert campaign["progress"]["outcome"] == "ok"
    page = next(p for p in detail["pages"] if p["url"].endswith("/long-title"))
    assert page["status"] == "EXTRACTED"
    assert len(page["title"]) == 300 and page["title"].endswith("...")


async def test_concurrent_playbook_saves_in_opposite_orders_do_not_deadlock(
    infra: Infrastructure, hclient: httpx.AsyncClient
) -> None:
    user = await _user(hclient)
    domains = [f"site{i}.example" for i in range(12)]
    now = infra.clock.now()
    books = {d: DomainPlaybook.new(user.id, d, now) for d in domains}

    async def save(order: list[str]) -> None:
        # The runner's save_playbooks: one transaction, rows upserted in SORTED order.
        for _ in range(5):
            async with infra.uow_factory(user.id) as uow:
                for d in sorted(order):
                    await uow.playbooks.save(books[d])
                await uow.commit()

    await asyncio.wait_for(
        asyncio.gather(save(domains), save(domains[::-1]), save(domains[3:] + domains[:3])),
        timeout=60,
    )


async def test_ready_reports_dependencies(hclient: httpx.AsyncClient) -> None:
    res = await hclient.get("/api/v1/ready")
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": True, "redis": True}


async def test_ready_is_503_when_the_database_is_down(
    infra: Infrastructure, hclient: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure.runtime import resources

    async def down(self: resources.DatabaseProbe) -> bool:
        return False

    monkeypatch.setattr(resources.DatabaseProbe, "check", down)
    res = await hclient.get("/api/v1/ready")
    assert res.status_code == 503
    assert res.json()["checks"]["database"] is False


def test_production_refuses_the_dev_login_bypass() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        dev_auth_bypass=True,
        database_url="postgresql+asyncpg://unused/unused",
        jwt_secret="x" * 48,  # type: ignore[arg-type]
        encryption_keys=f"1:{KEY_V1}",  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="DEV_AUTH_BYPASS"):
        settings.validate_for_runtime()
