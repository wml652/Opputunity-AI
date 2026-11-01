"""Durable execution: restart runs whose worker died, from their checkpoint.

Every running campaign and fill holds a short lease that its worker renews (LEASE_TTL). An
expired lease means the worker is gone:

* a RUNNING campaign is re-enqueued; the run resumes from its checkpoint (fetched pages are
  skipped, distilled-but-unextracted pages re-queued, a staging campaign only finishes staging);
* a FILLING application is re-enqueued; the fill restarts from its saved state (answers,
  stored fields, portal session and checkpoint) and still stops at the human OK gate;
* SUBMITTING is NEVER resumed (it may already have been submitted): the reaper marks it
  SUBMISSION_FAILED with a "check the portal" note, as before.

Automatic resumes are capped at MAX_RESUMES per run; past the cap the campaign fails and the
application is handed back to the user, with the reason."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.application.applications.submission import ReapLeases
from app.application.services.leases import LEASE_TTL, MAX_RESUMES
from app.domain.discovery.models import CampaignPhase
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import ApplicationState, CampaignStatus

Start = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]
# In-process mode at startup: every lease belonged to the process that just died.
RESTART_HORIZON = timedelta(days=3650)


@dataclass(frozen=True, slots=True)
class ResumeReport:
    campaigns_resumed: int = 0
    campaigns_failed: int = 0
    fills_resumed: int = 0
    reaped: int = 0


@dataclass(slots=True)
class ResumeInterruptedWork:
    system_uow_factory: IUnitOfWorkFactory
    clock: Clock
    start_campaign: Start
    start_application: Start
    reaper: ReapLeases
    max_resumes: int = MAX_RESUMES

    async def execute(self, *, process_restarted: bool = False) -> ResumeReport:
        now = self.clock.now()
        horizon: datetime = now + RESTART_HORIZON if process_restarted else now
        pending_until = now + LEASE_TTL
        campaigns_resumed = campaigns_failed = fills_resumed = 0

        to_start: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with self.system_uow_factory() as uow:
            for c in await uow.campaigns.stale_running(horizon):
                if c.resume_count >= self.max_resumes:
                    await uow.campaigns.update_state(
                        c.user_id,
                        c.id,
                        status=CampaignStatus.FAILED,
                        phase=CampaignPhase.FAILED,
                        error=(
                            f"interrupted {c.resume_count + 1} times (the worker kept stopping); "
                            "launch the campaign again"
                        ),
                        finished_at=now,
                    )
                    campaigns_failed += 1
                    continue
                await uow.campaigns.mark_resumed(c.user_id, c.id, pending_until)
                to_start.append((c.user_id, c.id))
            await uow.commit()
        for user_id, campaign_id in to_start:
            await self.start_campaign(user_id, campaign_id)
            campaigns_resumed += 1

        fills: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with self.system_uow_factory() as uow:
            for app, _opp in await uow.app_details.stale_leases(
                horizon, [ApplicationState.FILLING]
            ):
                if app.resume_count >= self.max_resumes:
                    continue  # the reaper hands it back to the user (Retry)
                await uow.app_details.mark_resumed(app.user_id, app.id, pending_until)
                fills.append((app.user_id, app.id))
            await uow.commit()
        for user_id, application_id in fills:
            await self.start_application(user_id, application_id)
            fills_resumed += 1

        # Over-cap fills -> BLOCKED; interrupted submissions -> SUBMISSION_FAILED (never retried).
        reaped = await self.reaper.execute(horizon, spare_pending_after=now)
        return ResumeReport(campaigns_resumed, campaigns_failed, fills_resumed, reaped)
