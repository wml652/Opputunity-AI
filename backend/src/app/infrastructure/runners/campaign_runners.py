from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.application.discovery.use_cases import RunCampaign
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class InProcessCampaignRunner:
    """Runs campaigns as background tasks inside the API process (dev / single-process mode).
    The API's Proactor loop supports Playwright's browser subprocess on Windows."""

    def __init__(self, run: RunCampaign) -> None:
        self._run = run
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        task = asyncio.create_task(
            self._guarded(user_id, campaign_id), name=f"campaign-{campaign_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        try:
            await self._run.execute(user_id, campaign_id)
        except Exception:
            log.exception("campaign.runner_crashed", campaign_id=str(campaign_id))

    async def wait_idle(self, timeout: float = 120.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class ArqCampaignRunner:
    """Enqueues `run_campaign` on the arq `discovery` queue (production / multi-process)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def start(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
        await self._pool.enqueue_job(
            "run_campaign",
            str(user_id),
            str(campaign_id),
            _queue_name="arq:discovery",
            _job_id=f"campaign:{campaign_id}",
        )
