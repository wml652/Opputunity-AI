"""Teach Mode: the student applies (or signs in) in a visible browser while the agent watches,
then the agent's brain keeps what it learned.

* Every recorded step streams to the UI as `agent.learned_step` (personal values masked).
* On finish: the navigation path to the form and the field mappings are saved as the portal's
  `agent_workflows` entry; each custom question -> answer goes into semantic memory
  (knowledge_memory_qa, with its embedding) so future applications answer it by themselves.
  Personal fields keep only their profile path (never their value); passwords are never
  recorded; answers containing personal data are not memorised.
* The session cookies are saved (encrypted) for the portal.
* If the student submitted the form themselves (confirmation page), the application is
  recorded as SUBMITTED by the user; otherwise the agent resumes filling with what it learned.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from app.application.applications.portal_sessions import portal_domain, saved_portal_state
from app.application.applications.use_cases import PII_FACT_KEYS, StartFilling, _transition
from app.application.memory.use_cases import LearnAnswer
from app.application.profile.use_cases import GetProfile
from app.application.services.field_mapper import match_profile_key
from app.application.training.service import TrajectoryService
from app.domain.agent.models import (
    AgentWorkflow,
    FieldMapping,
    StepKind,
    TeachStep,
    detect_ats,
    final_field_values,
    looks_submitted,
    navigation_path,
    workflow_domain,
)
from app.domain.applications.filling import PortalGate
from app.domain.discovery.listing import is_aggregator_url
from app.domain.discovery.sessions import BROWSER_SESSION_AAD, BrowserSession, SessionKind
from app.domain.events.models import NewEvent
from app.domain.ports.agent import ITeachRecorder, TeachResult
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.hitl import TEACH, HitlSession, IRemoteBrowserHub
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.profile.evidence import redact_pii
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor, ApplicationState
from app.domain.shared.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7
from app.domain.training.models import TrajectorySource, click_action, fill_action
from app.domain.training.privacy import profile_token

TEACH_TIMEOUT_S = 1_800.0
SESSION_TTL = timedelta(days=30)
PERSONAL = "(personal - never stored)"


@dataclass(frozen=True, slots=True)
class TeachDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    clock: Clock
    recorder: ITeachRecorder | None  # None in production / headless servers
    learn: LearnAnswer
    get_profile: GetProfile
    start: StartFilling
    spawn: Callable[[Coroutine[Any, Any, None]], None]
    training: TrajectoryService | None = None  # DOM-RAG memory + imitation-learning data
    # Servers: the browser runs headless and is streamed into the console (None = local window).
    hub: IRemoteBrowserHub | None = None


@dataclass(frozen=True, slots=True)
class TeachStarted:
    application_id: uuid.UUID
    url: str
    session_id: uuid.UUID | None = None  # remote browser session (servers)


@dataclass(frozen=True, slots=True)
class LearnedSummary:
    domain: str
    ats_type: str
    navigation_steps: int
    fields: int
    answers_learned: int
    submitted: bool


@dataclass(slots=True)
class TeachAgentSession:
    deps: TeachDeps
    active: dict[uuid.UUID, asyncio.Event] = field(default_factory=dict)
    remote: dict[uuid.UUID, HitlSession] = field(default_factory=dict)

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> TeachStarted:
        if self.deps.recorder is None:
            raise ValidationError(
                "Teach Mode needs a browser: install Playwright's Chromium on this machine (or "
                "enable remote browser sessions on the server)"
            )
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            checkpoint = await uow.checkpoints.latest(user_id, application_id)
        if found is None:
            raise NotFoundError("application not found")
        app, opp = found
        if app.state in (ApplicationState.SUBMITTED, ApplicationState.CANCELLED):
            raise ValidationError(f"the application is {app.state.value.lower()}")
        if not opp.portal_url:
            raise ValidationError("this opportunity has no portal URL to open")
        if application_id in self.active:
            raise ConflictError("Teach Mode is already open for this application")
        url = opp.portal_url
        gate = (app.last_error or "")[1:].split("]", 1)[0]
        if (
            checkpoint is not None
            and gate in (PortalGate.LOGIN.value, PortalGate.CHALLENGE.value)
            and not is_aggregator_url(checkpoint.checkpoint.url)
        ):
            url = checkpoint.checkpoint.url  # start on the sign-in page the agent stopped at
        state, _ = await saved_portal_state(
            self.deps.uow_factory, self.deps.cipher, self.deps.clock.now(), user_id, url
        )
        stop = asyncio.Event()
        self.active[application_id] = stop
        hitl: HitlSession | None = None
        if self.deps.hub is not None:
            hitl = HitlSession(
                id=new_uuid7(),
                user_id=user_id,
                kind=TEACH,
                title=opp.title,
                url=url,
                application_id=application_id,
                started_at=self.deps.clock.now().isoformat(),
            )
            await self.deps.hub.open(hitl)
            self.remote[application_id] = hitl
            await self._event(user_id, application_id, "hitl.session_opened", hitl.to_dict())
        self.deps.spawn(self._run(user_id, application_id, opp.title, url, state, stop, hitl))
        return TeachStarted(application_id, url, hitl.id if hitl else None)

    async def workflows(self, user_id: uuid.UUID) -> list[AgentWorkflow]:
        async with self.deps.uow_factory(user_id) as uow:
            return await uow.agent_workflows.list(user_id)

    async def finish(self, user_id: uuid.UUID, application_id: uuid.UUID) -> bool:
        """ "Finish training & save to brain" from the console (the in-page button does the
        same). False if no Teach Mode window is open for this application. A remote session is
        finished through the hub, so it works from any API replica."""
        stop = self.active.get(application_id)
        if stop is not None:
            stop.set()
            return True
        if self.deps.hub is not None:
            for session in await self.deps.hub.list(user_id):
                if session.kind == TEACH and session.application_id == application_id:
                    return await self.deps.hub.send_input(user_id, session.id, {"type": "finish"})
        return False

    async def _pii_values(self, user_id: uuid.UUID) -> list[str]:
        view = await self.deps.get_profile.execute(user_id)
        if view is None:
            return []
        values = [view.profile.full_name, *view.profile.full_name.split()]
        values += [v for v in view.profile.pii.to_dict().values() if v]
        return [v for v in values if v and len(v) >= 3]

    def _personal(self, step: TeachStep) -> bool:
        key = match_profile_key(step.field_label) or match_profile_key(
            step.field_name.replace("_", " ")
        )
        return step.secret or (key is not None and key in PII_FACT_KEYS)

    async def _run(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        title: str,
        url: str,
        state: str | None,
        stop: asyncio.Event,
        hitl: HitlSession | None = None,
    ) -> None:
        assert self.deps.recorder is not None
        pii = await self._pii_values(user_id)
        counter = [0]

        async def on_step(step: TeachStep) -> None:
            counter[0] += 1
            preview: str | None = None
            if step.value is not None:
                preview = (
                    PERSONAL
                    if self._personal(step) or redact_pii(step.value, pii) != step.value
                    else step.value[:80]
                )
            await self._event(
                user_id,
                application_id,
                "agent.learned_step",
                {
                    "title": title,
                    "n": counter[0],
                    "kind": step.kind.value,
                    "url": step.url,
                    "label": step.field_label or step.locator.name or step.locator.label,
                    "field_type": step.field_type,
                    "value_preview": "(password - never recorded)" if step.secret else preview,
                },
            )

        try:
            result = await self.deps.recorder.record(
                url,
                storage_state=state,
                on_step=on_step,
                timeout_s=TEACH_TIMEOUT_S,
                stop=stop,
                hitl=hitl,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user, never raised further
            await self._event(
                user_id, application_id, "agent.teach_failed", {"error": str(exc)[:300]}
            )
            return
        finally:
            self.active.pop(application_id, None)
            if hitl is not None and self.deps.hub is not None:
                self.remote.pop(application_id, None)
                await self.deps.hub.close(user_id, hitl.id)
                await self._event(
                    user_id, application_id, "hitl.session_closed", {"id": str(hitl.id)}
                )
        try:
            summary = await self._learn(user_id, application_id, result, pii)
        except DomainError as exc:
            await self._event(user_id, application_id, "agent.teach_failed", {"error": exc.message})
            return
        await self._event(
            user_id,
            application_id,
            "agent.workflow_saved",
            {
                "title": title,
                "domain": summary.domain,
                "ats_type": summary.ats_type,
                "navigation_steps": summary.navigation_steps,
                "fields": summary.fields,
                "answers_learned": summary.answers_learned,
                "submitted": summary.submitted,
            },
        )
        await self._after(user_id, application_id, summary, result)

    async def _learn(
        self, user_id: uuid.UUID, application_id: uuid.UUID, result: TeachResult, pii: list[str]
    ) -> LearnedSummary:
        mappings: list[FieldMapping] = []
        field_actions: list[tuple[TeachStep, str]] = []  # (step, value token) for trajectories
        learned = 0
        fields = final_field_values(result.steps)
        for step in fields:
            if step.secret:
                continue  # passwords are never recorded, let alone stored
            key = match_profile_key(step.field_label) or match_profile_key(
                step.field_name.replace("_", " ")
            )
            base = {
                "field_label": step.field_label,
                "field_name": step.field_name,
                "field_type": step.field_type,
            }
            if key is not None and key in PII_FACT_KEYS:
                mappings.append(FieldMapping(**base, mapped_profile_path=key))
                field_actions.append((step, profile_token(key)))
                continue
            if step.kind is StepKind.UPLOAD:
                mappings.append(FieldMapping(**base))
                field_actions.append((step, "{{DOCUMENT.FILE}}"))
                continue
            value = (step.value or "").strip()
            if key is not None:
                mappings.append(FieldMapping(**base, user_value=value, mapped_profile_path=key))
                field_actions.append((step, profile_token(key)))
                continue
            if not value or not step.field_label.strip() or redact_pii(value, pii) != value:
                mappings.append(FieldMapping(**base))  # personal content: not memorised
                field_actions.append((step, "{{USER.FREE_TEXT}}"))
                continue
            qa = await self.deps.learn.execute(user_id, question=step.field_label, answer=value)
            learned += 1
            mappings.append(FieldMapping(**base, user_value=value, memory_qa_id=str(qa.id)))
            field_actions.append((step, "{{MEMORY.QA_MATCH}}"))

        form_url = next(
            (s.url for s in result.steps if s.kind in (StepKind.FILL, StepKind.SELECT)),
            result.final_url,
        )
        domain = workflow_domain(form_url) or workflow_domain(result.final_url)
        ats = detect_ats(form_url)
        # Workflows keep the route only - page text (which may show personal data) is not kept.
        path = [replace(s, dom_signature="") for s in navigation_path(result.steps)]
        now = self.deps.clock.now()
        async with self.deps.uow_factory(user_id) as uow:
            existing = await uow.agent_workflows.find(user_id, domain)
            workflow = existing or AgentWorkflow(
                id=new_uuid7(),
                user_id=user_id,
                domain=domain,
                ats_type=ats,
                navigation_steps=[],
                field_mappings=[],
                source_application_id=application_id,
                created_at=now,
            )
            workflow.merge(path, mappings)
            workflow.source_application_id = application_id
            await uow.agent_workflows.upsert(workflow)
            if result.storage_state:
                await self._save_session(uow, user_id, domain, result.storage_state)
            await uow.commit()
        await self._trajectories(user_id, application_id, result, field_actions)
        return LearnedSummary(
            domain=domain,
            ats_type=ats.value,
            navigation_steps=len(path),
            fields=len(mappings),
            answers_learned=learned,
            submitted=looks_submitted(result.final_text, result.steps),
        )

    async def _trajectories(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        result: TeachResult,
        field_actions: list[tuple[TeachStep, str]],
    ) -> None:
        """Every demonstrated decision becomes a trajectory (DOM-RAG memory + SFT data). A
        demonstration on a page where the bot's own decision failed earlier becomes a DPO
        preference pair: chosen = the student's action, rejected = the bot's."""
        training = self.deps.training
        if training is None:
            return
        known = await training.known_values(user_id)
        async with self.deps.uow_factory(user_id) as uow:
            failures = await uow.trajectories.failures_for(user_id, application_id)

        def path_of(signature: str) -> str:
            return signature.split("\n", 1)[0]

        actions: list[tuple[TeachStep, dict[str, Any]]] = [
            (s, click_action(s.locator.name or s.locator.label, s.locator.role, s.locator.css))
            for s in result.steps
            if s.kind is StepKind.CLICK and s.dom_signature and (s.locator.name or s.locator.label)
        ]
        actions += [
            (s, fill_action(s.field_label or s.field_name, token, s.field_type, s.locator.css))
            for s, token in field_actions
            if s.dom_signature
        ]
        used: set[uuid.UUID] = set()
        for step, action in actions:
            rejected = None
            for failure in failures:
                same_page = path_of(failure.dom_signature) == path_of(step.dom_signature)
                if failure.id not in used and same_page:
                    bot = {k: v for k, v in failure.chosen_action.items() if k != "tier"}
                    if (
                        bot.get("label") != action.get("label")
                        or bot.get("action") != action["action"]
                    ):
                        rejected = bot
                        used.add(failure.id)
                        break
            await training.record(
                user_id,
                TrajectorySource.TEACH,
                url=step.url,
                signature=step.dom_signature,
                action=action,
                application_id=application_id,
                rejected=rejected,
                known=known,
            )

    async def _save_session(self, uow: Any, user_id: uuid.UUID, domain: str, state: str) -> None:
        domain = portal_domain(f"https://{domain}/") or domain
        existing = await uow.browser_sessions.find(user_id, domain, SessionKind.PORTAL)
        row_id = existing.id if existing else new_uuid7()
        now = self.deps.clock.now()
        await uow.browser_sessions.upsert(
            BrowserSession(
                id=row_id,
                user_id=user_id,
                domain=domain,
                kind=SessionKind.PORTAL,
                storage_state=self.deps.cipher.encrypt(
                    state.encode(), aad(BROWSER_SESSION_AAD, "storage_state", user_id, row_id)
                ),
                created_at=now,
                expires_at=now + SESSION_TTL,
            )
        )

    async def _after(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        summary: LearnedSummary,
        result: TeachResult,
    ) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                return
            app, opp = found
            if summary.submitted:
                await _transition(
                    uow,
                    user_id,
                    app,
                    ApplicationState.SUBMITTED,
                    actor=Actor.USER,
                    reason="you submitted this application yourself in Teach Mode",
                    at=self.deps.clock.now(),
                    title=opp.title,
                )
                await uow.commit()
                return
        if app.state in (ApplicationState.QUEUED, ApplicationState.BLOCKED_NEEDS_HUMAN):
            # The agent continues on its own with the learned path, cookies and answers.
            try:
                await self.deps.start.execute(user_id, application_id)
            except DomainError as exc:
                await self._event(
                    user_id, application_id, "agent.teach_failed", {"error": exc.message}
                )

    async def _event(
        self, user_id: uuid.UUID, application_id: uuid.UUID, type_: str, payload: dict[str, Any]
    ) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type=type_,
                    application_id=application_id,
                    payload={"application_id": str(application_id), **payload},
                )
            )
            await uow.commit()
