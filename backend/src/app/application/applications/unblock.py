""" "Sign In in Browser & Resume" for applications stuck at a portal login wall or challenge.

Opens a VISIBLE browser on the user's machine at the portal (`SessionKind.PORTAL`); the user
signs in themselves; the resulting cookies are stored encrypted for the portal's domain, and only
then is the application moved BLOCKED_NEEDS_HUMAN -> RESUME_QUEUED and the filler restarted - it
picks the saved session up automatically (`portal_sessions.saved_portal_state`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.applications.use_cases import StartFilling
from app.application.discovery.sessions import CaptureBrowserSession
from app.domain.applications.filling import PortalGate
from app.domain.discovery.listing import is_aggregator_url
from app.domain.discovery.sessions import SessionKind
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.enums import ApplicationState
from app.domain.shared.errors import NotFoundError, ValidationError


@dataclass(frozen=True, slots=True)
class UnblockStarted:
    application_id: uuid.UUID
    domain: str
    url: str


@dataclass(slots=True)
class UnblockApplication:
    uow_factory: IUnitOfWorkFactory
    capture: CaptureBrowserSession
    resume: StartFilling

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> UnblockStarted:
        async with self.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            checkpoint = await uow.checkpoints.latest(user_id, application_id)
        if found is None:
            raise NotFoundError("application not found")
        app, opp = found
        if app.state is not ApplicationState.BLOCKED_NEEDS_HUMAN:
            raise ValidationError(f"only blocked applications can be unblocked ({app.state.value})")
        if not opp.portal_url:
            raise ValidationError("this opportunity has no portal URL to sign in to")

        async def resume() -> None:
            await self.resume.execute(user_id, application_id)

        # A sign-in wall found by the pathfinder: open its sign-in page (the checkpoint URL), not
        # the overview / aggregator page the portal URL points at.
        url = opp.portal_url
        if (
            checkpoint is not None
            and (app.last_error or "").startswith(
                (f"[{PortalGate.LOGIN.value}]", f"[{PortalGate.CHALLENGE.value}]")
            )
            and not is_aggregator_url(checkpoint.checkpoint.url)
        ):
            url = checkpoint.checkpoint.url
        domain = await self.capture.execute(user_id, url, SessionKind.PORTAL, on_captured=resume)
        return UnblockStarted(application_id, domain, url)
