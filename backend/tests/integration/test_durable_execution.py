"""Track 1: durable execution - run leases, resume from checkpoint after a worker restart,
interrupted submissions never retried, global browser pool wired into fills."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import asyncpg
import httpx

from app.application.services.leases import PENDING_OWNER
from app.bootstrap.container import Infrastructure, build_resume
from tests.conftest import DatabaseUrls
from tests.fakes import FakeLLMFactory
from tests.integration import test_self_healing as heal
from tests.integration.test_application_filler import _queued_application
from tests.portal import HUB_DEALS, Portal

fake_llm = heal.fake_llm
hclient = heal.hclient
search_urls = heal.search_urls
_run, _user = heal._run, heal._user


def _extraction_calls(fake: FakeLLMFactory) -> int:
    return sum(
        1
        for llm in fake.created
        for call in llm.calls
        if "extract structured opportunity" in call[0].content
    )


async def _sql(dsn: str, query: str, *args: Any) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        if query.lstrip().upper().startswith("SELECT"):
            return await conn.fetch(query, *args)
        return await conn.execute(query, *args)
    finally:
        await conn.close()


async def _wait_status(user: Any, cid: str, *statuses: str) -> dict[str, Any]:
    for _ in range(600):
        detail: dict[str, Any] = (await user.get(f"/api/v1/campaigns/{cid}")).json()
        if detail["campaign"]["status"] in statuses:
            return detail
        await asyncio.sleep(0.1)
    raise AssertionError(detail["campaign"])


async def test_campaign_resumes_from_its_checkpoint_after_the_worker_died(
    hclient: httpx.AsyncClient,
    portal: Portal,
    fake_llm: FakeLLMFactory,
    infra: Infrastructure,
    clean_db: DatabaseUrls,
) -> None:
    user = await _user(hclient)
    urls = [f"{portal.base}/deal/{slug}" for slug in HUB_DEALS[:3]]
    first = await _run(user, target_urls=urls)
    cid = first["campaign"]["id"]
    assert first["campaign"]["status"] == "COMPLETED"
    opps_before = (
        await _sql(
            clean_db.owner,
            "SELECT count(*) AS n FROM opportunities WHERE campaign_id = $1",
            uuid.UUID(cid),
        )
    )[0]["n"]
    extractions_before = _extraction_calls(fake_llm)

    # The worker died mid-extraction: the last page was distilled but never extracted, and its
    # lease ran out.
    await _sql(
        clean_db.owner,
        "UPDATE campaigns SET status = 'RUNNING', phase = 'EXTRACTING', finished_at = NULL, "
        "lease_owner = 'dead-host:1:discovery/0', lease_expires_at = now() - interval '1 minute' "
        "WHERE id = $1",
        uuid.UUID(cid),
    )
    await _sql(
        clean_db.owner,
        "UPDATE crawled_pages SET status = 'DISTILLED' WHERE campaign_id = $1 AND url = $2",
        uuid.UUID(cid),
        urls[-1],
    )

    report = await build_resume(infra).execute()
    assert report.campaigns_resumed == 1
    done = await _wait_status(user, cid, "COMPLETED", "FAILED")
    assert done["campaign"]["status"] == "COMPLETED", done["campaign"]

    # Only the unfinished page was extracted again; nothing was duplicated.
    assert _extraction_calls(fake_llm) - extractions_before == 1
    opps_after = (
        await _sql(
            clean_db.owner,
            "SELECT count(*) AS n FROM opportunities WHERE campaign_id = $1",
            uuid.UUID(cid),
        )
    )[0]["n"]
    assert opps_after == opps_before
    row = (
        await _sql(
            clean_db.owner,
            "SELECT resume_count, lease_owner FROM campaigns WHERE id = $1",
            uuid.UUID(cid),
        )
    )[0]
    assert row["resume_count"] == 1 and row["lease_owner"] is None  # released when done
    events = (await user.get("/api/v1/events", params={"limit": 500})).json()
    resumed = [e["payload"] for e in events if e["type"] == "campaign.resumed"]
    assert resumed and resumed[-1]["pages_requeued"] == 1


async def test_a_live_lease_is_never_stolen_and_resumes_are_capped(
    hclient: httpx.AsyncClient,
    portal: Portal,
    infra: Infrastructure,
    clean_db: DatabaseUrls,
) -> None:
    user = await _user(hclient)
    first = await _run(user, target_urls=[f"{portal.base}/deal/{HUB_DEALS[0]}"])
    cid = uuid.UUID(first["campaign"]["id"])
    now = infra.clock.now()
    async with infra.uow_factory(user.id) as uow:
        assert await uow.campaigns.claim_lease(user.id, cid, "a/1", now, now + timedelta(minutes=2))
        assert not await uow.campaigns.claim_lease(
            user.id, cid, "b/1", now, now + timedelta(minutes=2)
        )
        assert await uow.campaigns.claim_lease(user.id, cid, "a/1", now, now + timedelta(minutes=2))
        later = now + timedelta(minutes=3)  # a's lease expired: b may take over
        assert await uow.campaigns.claim_lease(user.id, cid, "b/1", later, later)
        await uow.campaigns.mark_resumed(user.id, cid, later + timedelta(minutes=2))
        # queued for resume: any worker may claim it
        assert await uow.campaigns.claim_lease(user.id, cid, "c/1", later, later)
        await uow.commit()

    # A campaign that keeps dying is failed with the reason, not resumed forever.
    await _sql(
        clean_db.owner,
        "UPDATE campaigns SET status = 'RUNNING', resume_count = 3, lease_owner = 'x', "
        "lease_expires_at = now() - interval '1 minute' WHERE id = $1",
        cid,
    )
    report = await build_resume(infra).execute()
    assert report.campaigns_failed == 1 and report.campaigns_resumed == 0
    detail = (await user.get(f"/api/v1/campaigns/{cid}")).json()["campaign"]
    assert detail["status"] == "FAILED" and "interrupted 4 times" in detail["error"]


async def test_interrupted_submission_is_never_retried_and_over_cap_fills_go_to_the_user(
    hclient: httpx.AsyncClient,
    infra: Infrastructure,
    clean_db: DatabaseUrls,
) -> None:
    user = await _user(hclient)
    submitting = await _queued_application(infra, user, "https://portal.example/a", "Sub A")
    over_cap = await _queued_application(infra, user, "https://portal.example/b", "Fill B")
    pending = await _queued_application(infra, user, "https://portal.example/c", "Fill C")
    await _sql(
        clean_db.owner,
        "UPDATE applications SET state = 'SUBMITTING', lock_owner = 'dead', "
        "lease_expires_at = now() - interval '1 minute' WHERE id = $1",
        submitting,
    )
    await _sql(
        clean_db.owner,
        "UPDATE applications SET state = 'FILLING', resume_count = 3, lock_owner = 'dead', "
        "lease_expires_at = now() - interval '1 minute' WHERE id = $1",
        over_cap,
    )
    # Resumed a moment ago and still waiting for a worker: left alone.
    await _sql(
        clean_db.owner,
        "UPDATE applications SET state = 'FILLING', resume_count = 1, lock_owner = $2, "
        "lease_expires_at = now() + interval '1 minute' WHERE id = $1",
        pending,
        PENDING_OWNER,
    )
    started: list[uuid.UUID] = []

    async def no_start(_u: uuid.UUID, app_id: uuid.UUID) -> None:
        started.append(app_id)

    resume = build_resume(infra)
    resume.start_application = no_start
    report = await resume.execute()
    assert report.fills_resumed == 0 and started == [] and report.reaped == 2
    states = {
        r["id"]: (r["state"], r["last_error"])
        for r in await _sql(
            clean_db.owner,
            "SELECT id, state::text AS state, last_error FROM applications WHERE user_id = $1",
            user.id,
        )
    }
    assert states[submitting][0] == "SUBMISSION_FAILED"
    assert "may already have been submitted" in (states[submitting][1] or "")
    assert states[over_cap][0] == "BLOCKED_NEEDS_HUMAN"
    assert states[pending][0] == "FILLING"


async def test_application_lease_is_exclusive(
    hclient: httpx.AsyncClient, infra: Infrastructure
) -> None:
    user = await _user(hclient)
    app_id = await _queued_application(infra, user, "https://portal.example/x", "Lease X")
    now = infra.clock.now()
    async with infra.uow_factory(user.id) as uow:
        details = uow.app_details
        assert await details.claim_lease(user.id, app_id, "w1/a", now, now + timedelta(minutes=2))
        assert not await details.claim_lease(
            user.id, app_id, "w2/b", now, now + timedelta(minutes=2)
        )
        await details.mark_resumed(user.id, app_id, now + timedelta(minutes=2))
        assert await details.claim_lease(user.id, app_id, "w2/b", now, now + timedelta(minutes=2))
        await uow.commit()
    rows = await _sql_rows(infra, user.id, app_id)
    assert rows == ("w2/b", 1)


async def _sql_rows(infra: Infrastructure, user_id: uuid.UUID, app_id: uuid.UUID) -> Any:
    from sqlalchemy import text

    async with infra.uow_factory(user_id) as uow:
        row = (
            await uow.session.execute(  # type: ignore[attr-defined]
                text("SELECT lock_owner, resume_count FROM applications WHERE id = :id"),
                {"id": app_id},
            )
        ).one()
    return (row[0], row[1])


def test_settings_accept_the_old_campaign_runner_name() -> None:
    from app.infrastructure.config.settings import Settings
    from tests.conftest import KEY_V1

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        campaign_runner="arq",
        encryption_keys=f"1:{KEY_V1}",  # type: ignore[arg-type]
    )
    assert s.execution_mode == "arq"
