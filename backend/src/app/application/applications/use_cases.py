"""Application filling lifecycle: start -> fill -> pause for answers -> resume -> ready for OK.

Never submits: filling stops at the final step (AWAITING_FINAL_VERIFICATION); submission needs
the explicit human OK gate (Milestone 5)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.application.agent.navigator import llm_navigator
from app.application.applications.live import live_observer
from app.application.applications.portal_sessions import (
    combine_with_checkpoint,
    saved_portal_state,
)
from app.application.discovery.portal_search import OfficialPortalSearch
from app.application.essay.synthesis import EssayRequest, SynthesizeEssay
from app.application.llm.use_cases import ResolveChatProvider
from app.application.memory.use_cases import LearnAnswer, RecallMemory, UpdateMemory
from app.application.notifications.use_cases import NotifyUser
from app.application.profile.use_cases import DownloadDocument, GetProfile
from app.application.services.evidence import ProfileEvidenceRetriever
from app.application.services.field_mapper import DocumentRef, FieldMapper, normalize_label
from app.application.services.leases import LEASE_TTL, kept_lease, run_owner
from app.application.training.service import TrajectoryService
from app.domain.agent.models import detect_ats, workflow_domain
from app.domain.applications.filling import (
    ApplicationField,
    Checkpoint,
    CheckpointData,
    DocumentBlob,
    FieldResolution,
    FieldSource,
    FillOutcome,
    FillOutcomeKind,
    FormField,
    PendingQuestion,
    PortalGate,
    ResumeState,
)
from app.domain.applications.models import Application, Transition, advance
from app.domain.discovery.listing import is_aggregator_url
from app.domain.discovery.models import Opportunity
from app.domain.essay.synthesis import EssayDraft
from app.domain.events.models import NewEvent
from app.domain.llm.provider import EncryptedSecret
from app.domain.memory.models import normalize_question
from app.domain.notification.models import Template
from app.domain.ports.agent import DomRecall
from app.domain.ports.applications import (
    IApplicationRunner,
    IFormFiller,
    ILiveFrameStore,
    StoredCheckpoint,
    StoredField,
)
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.uow import IUnitOfWork, IUnitOfWorkFactory
from app.domain.ports.vault import IObjectStorage
from app.domain.profile.evidence import EvidenceAnswer
from app.domain.profile.models import PII_V2_FIELDS, ProfileDetails
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor, ApplicationState, QuestionStatus
from app.domain.shared.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7
from app.domain.training.models import Demonstration, TrajectorySource

S = ApplicationState
MAX_BATCH = 25
# Profile facts that are PII: redacted from every evidence snippet before a model sees it.
PII_FACT_KEYS = frozenset(
    {
        "full_name",
        "first_name",
        "last_name",
        "email",
        "home_address",
        "phone",
        "date_of_birth",
        "national_id",
        "passport_number",
        # profile v2 personal data: city, links, emergency contact, referees 1-3
        *PII_V2_FIELDS,
    }
)
FIELDS_TABLE = "application_fields"
CHECKPOINT_TABLE = "application_checkpoints"


@dataclass(frozen=True, slots=True)
class ApplicationDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    clock: Clock
    storage: IObjectStorage
    filler: IFormFiller
    get_profile: GetProfile
    download: DownloadDocument
    recall: RecallMemory
    learn: LearnAnswer
    update_memory: UpdateMemory
    essays: SynthesizeEssay
    notify: NotifyUser
    evidence: ProfileEvidenceRetriever | None = None  # CV / transcript / essay evidence RAG
    # Last resort for aggregator-only / dead portals: one web search for the official page.
    portal_search: OfficialPortalSearch | None = None
    aggregators: frozenset[str] = frozenset()
    live_frames: ILiveFrameStore | None = None  # live viewport (latest frame per application)
    # The user's chat model drives the LLM navigator when the pathfinder is stuck.
    chat: ResolveChatProvider | None = None
    # DOM-RAG memory + trajectory recording (imitation-learning data).
    training: TrajectoryService | None = None
    # Identity of this process in run leases (host:pid[:queue]); set by the composition root.
    runner_id: str = "in-process"


async def _transition(
    uow: IUnitOfWork,
    user_id: uuid.UUID,
    app: Application,
    target: ApplicationState,
    *,
    actor: Actor,
    reason: str,
    at: Any,
    error: str | None = None,
    title: str = "",
) -> Transition:
    expected = app.version
    transition = advance(app, target, actor=actor, reason=reason, at=at)
    if not await uow.app_details.save_state(user_id, app, expected, last_error=error):
        raise ConflictError("the application changed concurrently; retry")
    await uow.applications.add_transition(user_id, transition)
    await uow.events.append(
        NewEvent(
            user_id=user_id,
            type="application.state",
            application_id=app.id,
            payload={
                "application_id": str(app.id),
                "from": transition.from_state.value if transition.from_state else None,
                "state": target.value,
                "reason": reason,
                "title": title,
            },
        )
    )
    return transition


def _field_aad(user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    return aad(FIELDS_TABLE, "value", user_id, row_id)


@dataclass(slots=True)
class RunApplication:
    """One filling run (worker side). Resumable: called again after answers arrive."""

    deps: ApplicationDeps

    async def execute(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> FillOutcomeKind | None:
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            app, opp = found
            now = self.deps.clock.now()
            if app.state not in (S.QUEUED, S.RESUME_QUEUED, S.FILLING):
                return None
            # Worker lease (renewed while the run lasts): exactly one live run per application;
            # if this process dies the resume sweep restarts the fill from its saved state.
            owner = run_owner(self.deps.runner_id)
            if not await uow.app_details.claim_lease(user_id, app.id, owner, now, now + LEASE_TTL):
                return None  # another worker is filling it right now
            if app.state is S.QUEUED:
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.PREPARING,
                    actor=Actor.WORKER,
                    reason="preparing",
                    at=now,
                    title=opp.title,
                )
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.FILLING,
                    actor=Actor.WORKER,
                    reason="filling portal",
                    at=now,
                    title=opp.title,
                )
            elif app.state is S.RESUME_QUEUED:
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.FILLING,
                    actor=Actor.WORKER,
                    reason="resuming",
                    at=now,
                    title=opp.title,
                )
            await uow.commit()

        async def renew() -> None:
            at = self.deps.clock.now()
            async with self.deps.uow_factory(user_id) as uow:
                await uow.app_details.claim_lease(user_id, app.id, owner, at, at + LEASE_TTL)
                await uow.commit()

        try:
            async with kept_lease(renew):
                return await self._run(user_id, app, opp)
        finally:
            async with self.deps.uow_factory(user_id) as uow:
                await uow.app_details.set_lease(user_id, app.id, None, None)
                await uow.commit()

    async def _run(
        self, user_id: uuid.UUID, app: Application, opp: Opportunity
    ) -> FillOutcomeKind | None:
        searched = False
        if is_aggregator_url(opp.portal_url, self.deps.aggregators) or not opp.portal_url:
            # Aggregator-only (no outbound link was found): look the official page up first.
            searched = True
            opp = await self._search_portal(user_id, opp, "the portal is an aggregator page")
        if not opp.portal_url:
            await self._finish_blocked(
                user_id, app, opp, "the opportunity has no application portal URL"
            )
            return FillOutcomeKind.BLOCKED
        while True:
            try:
                outcome = await self._fill(user_id, app, opp)
            except DomainError as exc:
                await self._finish_blocked(user_id, app, opp, exc.message)
                return FillOutcomeKind.BLOCKED
            except Exception as exc:  # noqa: BLE001 - the run must always leave a visible state
                message = f"{type(exc).__name__}: {str(exc)[:300]}"
                if not searched and _is_navigation_failure(message):
                    searched = True
                    found = await self._search_portal(user_id, opp, "the portal did not load")
                    if found.portal_url != opp.portal_url:
                        opp = found
                        continue
                await self._finish_blocked(user_id, app, opp, message)
                return FillOutcomeKind.ERROR
            dead_end = outcome.kind is FillOutcomeKind.BLOCKED and outcome.gate in (
                PortalGate.AGGREGATOR.value,
                PortalGate.NO_FORM.value,
            )
            empty = outcome.checkpoint is None or not outcome.checkpoint.fill_ledger
            if dead_end and empty and not searched:
                searched = True
                found = await self._search_portal(user_id, opp, f"pathfinder: {outcome.gate}")
                if found.portal_url != opp.portal_url:
                    opp = found
                    continue
            await self._finish(user_id, app, opp, outcome)
            return outcome.kind

    async def _search_portal(self, user_id: uuid.UUID, opp: Opportunity, why: str) -> Opportunity:
        """One web search for the official application page; persists the better portal URL
        (and tells the UI) or returns the opportunity unchanged."""
        if self.deps.portal_search is None:
            return opp
        new = await self.deps.portal_search.find(
            opp.title, opp.organization, not_url=opp.portal_url
        )
        if not new or new == opp.portal_url:
            return opp
        async with self.deps.uow_factory(user_id) as uow:
            await uow.opportunities.set_portal_url(user_id, opp.id, new)
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="opportunity.portal_resolved",
                    payload={
                        "opportunity_id": str(opp.id),
                        "title": opp.title,
                        "old": opp.portal_url,
                        "new": new,
                        "via": "search",
                        "why": why,
                    },
                )
            )
            await uow.commit()
        return replace(opp, portal_url=new)

    async def _email_application(
        self, user_id: uuid.UUID, app: Application, opp: Opportunity, outcome: FillOutcome
    ) -> FillOutcome:
        """Applications by email / document package: draft the student's statement of purpose
        (stylometric engine, attached to this application) so it is ready to copy, and tell the
        user where to send it. Nothing is ever emailed automatically."""
        recipients = [str(r) for r in outcome.gate_info.get("recipients", [])][:3]
        draft = None
        try:
            draft = await self.deps.essays.execute(
                user_id,
                EssayRequest(
                    prompt=(
                        f"Statement of purpose / application email for {opp.title}"
                        + (f" at {opp.organization}" if opp.organization else "")
                    ),
                    word_limit=450,
                    application_id=app.id,
                    opportunity_id=opp.id,
                    field_key="email_application",
                    ai_policy=opp.ai_policy,
                    organization=opp.organization,
                ),
            )
        except DomainError:
            draft = None
        where = (
            f"email your application to {', '.join(recipients)}"
            if recipients
            else "send the application package described on the official page"
            + (
                f" (form: {outcome.gate_info['form_url']})"
                if outcome.gate_info.get("form_url")
                else ""
            )
        )
        ready = "; a statement-of-purpose draft is ready to copy" if draft is not None else ""
        message = f"[{PortalGate.EMAIL.value}] No online form: {where}{ready}"
        return replace(outcome, error=message[:500])

    # -- filling ---------------------------------------------------------------------------------

    async def _fill(self, user_id: uuid.UUID, app: Application, opp: Opportunity) -> FillOutcome:
        facts = await self._facts(user_id)
        async with self.deps.uow_factory(user_id) as uow:
            documents = await uow.documents.list(user_id)
            stored_fields = await uow.fields.for_application(user_id, app.id)
            checkpoint = await uow.checkpoints.latest(user_id, app.id)
        known: dict[str, FieldResolution] = {}
        rows: dict[str, ApplicationField] = {}
        for stored in stored_fields:
            f = stored.field
            rows[f.field_key] = f
            if stored.value is not None:
                value = self.deps.cipher.decrypt(stored.value, _field_aad(user_id, f.id)).decode()
                confidence = 1.0 if f.source is FieldSource.USER else f.confidence
                known[f.field_key] = FieldResolution(
                    f.field_key,
                    value,
                    f.source,
                    confidence,
                    f.memory_qa_id,
                    f.document_id,
                    f.essay_draft_id,
                    note="settled",
                )
        blobs: dict[uuid.UUID, DocumentBlob] = {}
        for doc in documents:
            _, data = await self.deps.download.execute(user_id, doc.id)
            blobs[doc.id] = DocumentBlob(doc.id, doc.filename, data)

        async def recall(question: str) -> Any:
            return await self.deps.recall.execute(user_id, question, k=3)

        async def essay(f: FormField) -> EssayDraft | None:
            async with self.deps.uow_factory(user_id) as uow2:
                existing = await uow2.essay_drafts.for_field(user_id, app.id, f.key)
            if existing is not None:
                return existing
            try:
                return await self.deps.essays.execute(
                    user_id,
                    EssayRequest(
                        prompt=f.label,
                        word_limit=(f.max_length // 6) if f.max_length else None,
                        application_id=app.id,
                        opportunity_id=opp.id,
                        field_key=f.key,
                        ai_policy=opp.ai_policy,
                        organization=opp.organization,
                    ),
                )
            except DomainError:
                return None

        pii_values = [v for k, v in facts.items() if k in PII_FACT_KEYS and v]

        async def evidence(f: FormField, memory: list[tuple[str, str, float]]) -> EvidenceAnswer:
            assert self.deps.evidence is not None
            return await self.deps.evidence.answer(
                user_id, f.label, options=f.options, pii_values=pii_values, memory=memory
            )

        # The agent brain: a route learned in Teach Mode, and (Tier 1) the profile facts the
        # student mapped to this portal's labels.
        async with self.deps.uow_factory(user_id) as uow:
            workflow = await uow.agent_workflows.find(
                user_id, workflow_domain(opp.portal_url or ""), detect_ats(opp.portal_url or "")
            )
        learned = {
            key: m.mapped_profile_path
            for m in (workflow.field_mappings if workflow else [])
            if m.mapped_profile_path
            for key in (normalize_label(m.field_label), m.field_name.lower())
            if key
        }
        mapper = FieldMapper(
            facts=facts,
            documents=[DocumentRef(d.id, d.kind, d.filename) for d in documents],
            recall=recall,
            essay=essay,
            known=known,
            evidence=evidence if self.deps.evidence is not None else None,
            learned=learned,
        )

        async def resolver(step_index: int, fields: list[FormField]) -> list[FieldResolution]:
            resolutions = [await mapper.resolve(f) for f in fields]
            async with self.deps.uow_factory(user_id) as uow3:
                for f, res in zip(fields, resolutions, strict=True):
                    row = rows.get(f.key)
                    row_id = row.id if row else new_uuid7()
                    keep_user = (
                        row is not None
                        and row.source is FieldSource.USER
                        and res.source is FieldSource.USER
                    )
                    field_row = ApplicationField(
                        id=row_id,
                        user_id=user_id,
                        application_id=app.id,
                        step_index=step_index,
                        field_key=f.key,
                        label=f.label,
                        field_type=f.field_type,
                        options=f.options,
                        required=f.required,
                        value=None,
                        source=res.source,
                        confidence=res.confidence,
                        memory_qa_id=res.memory_qa_id,
                        document_id=res.document_id,
                        essay_draft_id=res.essay_draft_id,
                        user_edited=keep_user,
                        updated_at=self.deps.clock.now(),
                        evidence=res.evidence,
                    )
                    secret = (
                        self.deps.cipher.encrypt(res.value.encode(), _field_aad(user_id, row_id))
                        if res.value is not None
                        else None
                    )
                    await uow3.fields.upsert(StoredField(field_row, secret))
                    rows[f.key] = field_row
                await uow3.commit()
            return resolutions

        assert opp.portal_url is not None
        # Cookies from a portal sign-in / solved challenge the user did in a visible browser.
        session_state, session_at = await saved_portal_state(
            self.deps.uow_factory,
            self.deps.cipher,
            self.deps.clock.now(),
            user_id,
            opp.portal_url,
            *([checkpoint.checkpoint.url] if checkpoint is not None else []),
        )
        resume = None
        # Resume only where something was filled; an empty checkpoint (a gate, a dead end, an
        # old aggregator page) restarts from the - possibly repaired - portal URL instead.
        if checkpoint is not None and checkpoint.checkpoint.fill_ledger:
            state = self.deps.cipher.decrypt(
                checkpoint.storage_state,
                aad(CHECKPOINT_TABLE, "storage_state", user_id, checkpoint.checkpoint.id),
            ).decode()
            resume = ResumeState(
                url=checkpoint.checkpoint.url,
                step_index=checkpoint.checkpoint.step_index,
                dom_fingerprint=checkpoint.checkpoint.dom_fingerprint,
                storage_state=combine_with_checkpoint(
                    state, checkpoint.checkpoint.created_at, session_state, session_at
                )
                or state,
            )
        navigator = None
        if self.deps.chat is not None:
            try:
                navigator = llm_navigator(await self.deps.chat.execute(user_id), pii_values)
            except DomainError:
                navigator = None
        dom_recall, decisions = await self._experience(user_id)
        outcome = await self.deps.filler.run(
            opp.portal_url,
            resume=resume,
            resolver=resolver,
            documents=blobs,
            storage_state=session_state,
            learned_path=workflow.navigation_steps if workflow else (),
            navigator=navigator,
            dom_recall=dom_recall,
            on_decision=decisions.append_decision if decisions else None,
            observer=live_observer(
                self.deps.uow_factory,
                self.deps.live_frames,
                self.deps.clock,
                user_id,
                app.id,
                opp.title,
                "FILLING",
            ),
        )
        if workflow is not None:
            async with self.deps.uow_factory(user_id) as uow:
                await uow.agent_workflows.record_use(
                    user_id, workflow.id, outcome.kind is FillOutcomeKind.READY_TO_SUBMIT
                )
                await uow.commit()
        if decisions:
            await decisions.save(app.id, outcome.kind)
        return outcome

    async def _experience(self, user_id: uuid.UUID) -> tuple[DomRecall | None, _DecisionLog | None]:
        """DOM-RAG recall (Tier 2 / few-shot for Tier 3) and the decision log of this run."""
        training = self.deps.training
        if training is None:
            return None, None
        known = await training.known_values(user_id)

        async def recall(signature: str) -> list[Demonstration]:
            return await training.recall(user_id, signature, known)

        return recall, _DecisionLog(training, user_id, known)

    async def _facts(self, user_id: uuid.UUID) -> dict[str, str]:
        view = await self.deps.get_profile.execute(user_id)
        async with self.deps.uow_factory(user_id) as uow:
            user = await uow.users.get(user_id)
        facts: dict[str, str] = (
            {"email": user.email} if user and "@opportunity.local" not in user.email else {}
        )
        if view is None:
            return facts
        p = view.profile
        parts = p.full_name.split()
        facts |= {
            "full_name": p.full_name,
            "first_name": parts[0] if parts else "",
            "last_name": parts[-1] if len(parts) > 1 else "",
            "nationality": p.nationality or "",
            "country_of_residence": p.country_of_residence or "",
            "institution": p.institution or "",
            "major": p.major or "",
            "degree_level": p.degree_level.value if p.degree_level else "",
            "gpa": f"{p.gpa.value.normalize():f}" if p.gpa else "",
            "graduation_year": str(p.graduation_year or ""),
            "skills": ", ".join(p.skills),
            "languages": ", ".join(f"{lang.name} ({lang.level})" for lang in p.languages),
            **{k: v for k, v in p.pii.to_dict().items() if v},
            **profile_v2_facts(p.details),
        }
        return {k: v for k, v in facts.items() if v}

    # -- outcomes --------------------------------------------------------------------------------

    async def _save_checkpoint(
        self, user_id: uuid.UUID, app: Application, data: CheckpointData
    ) -> Checkpoint:
        row_id = new_uuid7()
        secret = self.deps.cipher.encrypt(
            data.storage_state.encode(), aad(CHECKPOINT_TABLE, "storage_state", user_id, row_id)
        )
        shot_key = shot_nonce = shot_version = None
        if data.screenshot_png:
            shot = self.deps.cipher.encrypt(
                data.screenshot_png, aad(CHECKPOINT_TABLE, "screenshot", user_id, row_id)
            )
            shot_key = f"{user_id}/checkpoints/{row_id}.bin"
            await self.deps.storage.put(shot_key, shot.ciphertext)
            shot_nonce, shot_version = shot.nonce, shot.key_version
        async with self.deps.uow_factory(user_id) as uow:
            seq = await uow.checkpoints.next_seq(user_id, app.id)
            checkpoint = Checkpoint(
                id=row_id,
                user_id=user_id,
                application_id=app.id,
                seq=seq,
                step_index=data.step_index,
                url=data.url,
                dom_fingerprint=data.dom_fingerprint,
                fill_ledger=data.fill_ledger,
                has_screenshot=shot_key is not None,
                created_at=self.deps.clock.now(),
            )
            await uow.checkpoints.add(
                StoredCheckpoint(checkpoint, secret, shot_key, shot_nonce, shot_version)
            )
            await uow.commit()
        return checkpoint

    async def _finish(
        self, user_id: uuid.UUID, app: Application, opp: Opportunity, outcome: FillOutcome
    ) -> None:
        if outcome.gate == PortalGate.EMAIL.value:
            outcome = await self._email_application(user_id, app, opp, outcome)
        checkpoint = (
            await self._save_checkpoint(user_id, app, outcome.checkpoint)
            if outcome.checkpoint
            else None
        )
        now = self.deps.clock.now()
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, app.id)
            assert found is not None
            app = found[0]
            if outcome.kind is FillOutcomeKind.PAUSED:
                created = 0
                for item in outcome.unresolved:
                    created += await uow.questions.add(
                        PendingQuestion(
                            id=new_uuid7(),
                            user_id=user_id,
                            application_id=app.id,
                            field_key=item.field.key,
                            step_index=item.step_index,
                            question_text=item.field.label,
                            context=item.context,
                            field_type=item.field.field_type,
                            options=item.field.options,
                            suggested_answer=item.resolution.suggestion,
                            confidence=item.resolution.confidence,
                            status=QuestionStatus.OPEN,
                            answered_qa_id=None,
                            created_at=now,
                        )
                    )
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.PAUSED_NEEDS_USER_INPUT,
                    actor=Actor.WORKER,
                    reason=f"{len(outcome.unresolved)} question(s) need your answer",
                    at=now,
                    title=opp.title,
                )
                await uow.events.append(
                    NewEvent(
                        user_id=user_id,
                        type="inbox.questions",
                        application_id=app.id,
                        payload={
                            "application_id": str(app.id),
                            "title": opp.title,
                            "questions": len(outcome.unresolved),
                        },
                    )
                )
            elif outcome.kind is FillOutcomeKind.READY_TO_SUBMIT:
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.AWAITING_FINAL_VERIFICATION,
                    actor=Actor.WORKER,
                    reason="filled up to the final step; waiting for your OK",
                    at=now,
                    title=opp.title,
                )
            else:
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.BLOCKED_NEEDS_HUMAN,
                    actor=Actor.WORKER,
                    reason=outcome.error or "blocked",
                    at=now,
                    error=outcome.error,
                    title=opp.title,
                )
            await uow.commit()
        template = {
            FillOutcomeKind.PAUSED: Template.QUESTION_NEEDED,
            FillOutcomeKind.READY_TO_SUBMIT: Template.READY_FOR_OK,
        }.get(outcome.kind, Template.FAILED)
        await self.deps.notify.execute(
            user_id,
            template,
            {
                "title": opp.title,
                "questions": len(outcome.unresolved),
                "path": f"/applications/{app.id}",
            },
            application_id=app.id,
            state=f"{outcome.kind.value}:{checkpoint.seq if checkpoint else 0}",
        )

    async def _finish_blocked(
        self, user_id: uuid.UUID, app: Application, opp: Opportunity, error: str
    ) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, app.id)
            if found is None:
                return
            current = found[0]
            if current.state is S.FILLING:
                await _transition(
                    uow,
                    user_id,
                    current,
                    S.BLOCKED_NEEDS_HUMAN,
                    actor=Actor.WORKER,
                    reason=error[:200],
                    at=self.deps.clock.now(),
                    error=error,
                    title=opp.title,
                )
                await uow.commit()
        await self.deps.notify.execute(
            user_id,
            Template.FAILED,
            {"title": opp.title, "path": f"/applications/{app.id}"},
            application_id=app.id,
            state=f"BLOCKED:{error[:40]}",
        )


@dataclass(slots=True)
class StartFilling:
    """User clicks "Start filling" (QUEUED) or "Retry" (BLOCKED_NEEDS_HUMAN)."""

    uow_factory: IUnitOfWorkFactory
    clock: Clock
    runner: IApplicationRunner

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        async with self.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            app, opp = found
            if app.state is S.BLOCKED_NEEDS_HUMAN:
                await _transition(
                    uow,
                    user_id,
                    app,
                    S.RESUME_QUEUED,
                    actor=Actor.USER,
                    reason="retry requested",
                    at=self.clock.now(),
                    title=opp.title,
                )
                await uow.commit()
            elif app.state not in (S.QUEUED, S.RESUME_QUEUED):
                raise ValidationError(f"cannot start filling from state {app.state.value}")
        await self.runner.start(user_id, application_id)


@dataclass(frozen=True, slots=True)
class BatchResult:
    started: list[uuid.UUID]
    skipped_no_portal: int


@dataclass(slots=True)
class BatchPrepare:
    """ "Auto-Stage Eligible Queue": fill the top-N QUEUED applications (closest deadline first)
    up to AWAITING_FINAL_VERIFICATION or PAUSED_NEEDS_USER_INPUT. NEVER submits: every one of
    them still needs the human "OK - Verify & Submit"."""

    uow_factory: IUnitOfWorkFactory
    runner: IApplicationRunner

    async def execute(
        self,
        user_id: uuid.UUID,
        limit: int = 5,
        *,
        campaign_id: uuid.UUID | None = None,
        exclude: frozenset[uuid.UUID] = frozenset(),
    ) -> BatchResult:
        if not 1 <= limit <= MAX_BATCH:
            raise ValidationError(f"limit must be 1-{MAX_BATCH}")
        async with self.uow_factory(user_id) as uow:
            queued = await uow.app_details.list_by_states(user_id, [S.QUEUED])  # deadline ASC
        if campaign_id is not None:
            queued = [(app, opp) for app, opp in queued if opp.campaign_id == campaign_id]
        queued = [(app, opp) for app, opp in queued if app.id not in exclude]
        with_portal = [(app, opp) for app, opp in queued if opp.portal_url]
        picked = with_portal[:limit]
        for app, _ in picked:
            await self.runner.start(user_id, app.id)
        return BatchResult(
            [app.id for app, _ in picked], skipped_no_portal=len(queued) - len(with_portal)
        )


_NAVIGATION_FAILURE = ("Page.goto", "net::ERR_", "TimeoutError", "ERR_NAME_NOT_RESOLVED")


class _DecisionLog:
    """The agent's decisions during one run; stored once the outcome is known - success
    (reached Ready for OK / a question) or failure (blocked) - as trajectories."""

    def __init__(
        self, training: TrajectoryService, user_id: uuid.UUID, known: dict[str, str]
    ) -> None:
        self.training = training
        self.user_id = user_id
        self.known = known
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __bool__(self) -> bool:  # an empty log is still a log
        return True

    def append_decision(self, signature: str, action: dict[str, Any]) -> None:
        self.items.append((signature, dict(action)))

    async def save(self, application_id: uuid.UUID, kind: FillOutcomeKind) -> None:
        source = (
            TrajectorySource.AUTO_FAILURE
            if kind in (FillOutcomeKind.BLOCKED, FillOutcomeKind.ERROR)
            else TrajectorySource.AUTO_SUCCESS
        )
        for signature, action in self.items:
            url = str(action.pop("url", "") or "")
            await self.training.record(
                self.user_id,
                source,
                url=url,
                signature=signature,
                action=action,
                application_id=application_id,
                known=self.known,
            )


def _is_navigation_failure(message: str) -> bool:
    return any(marker in message for marker in _NAVIGATION_FAILURE)


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("Yes" if value else "No")


def profile_v2_facts(d: ProfileDetails) -> dict[str, str]:
    """Form-filler facts from profile v2 (non-PII part; the PII part comes from StudentPII)."""
    english = ""
    if d.english_proficiency_type is not None:
        kind = d.english_proficiency_type.value
        english = {
            "NATIVE": "Native speaker",
            "MOI_EXEMPT": "English-medium instruction (MOI)",
            "NONE": "",
        }.get(kind, f"{kind} {d.english_proficiency_score or ''}".strip())
    facts = {
        "current_semester_or_year": d.current_semester_or_year or "",
        "cgpa_percentage": f"{d.cgpa_percentage:g}" if d.cgpa_percentage is not None else "",
        "secondary_school_qualification": d.secondary_school_qualification or "",
        "secondary_school_percentage": (
            f"{d.secondary_school_percentage:g}"
            if d.secondary_school_percentage is not None
            else ""
        ),
        "minor_or_specialization": d.minor_or_specialization or "",
        "thesis_or_fyp_title": d.thesis_or_fyp_title or "",
        "academic_honors": ", ".join(d.academic_honors),
        "english_proficiency": english,
        "research_interests": ", ".join(d.research_interests),
        "publications_count": str(d.publications_count) if d.publications_count is not None else "",
        "preferred_work_mode": d.preferred_work_mode.value.title() if d.preferred_work_mode else "",
        "earliest_start_date": d.earliest_start_date or "",
        "requires_visa_sponsorship": _yes_no(d.requires_visa_sponsorship),
        "has_valid_passport": _yes_no(d.has_valid_passport),
        "financial_need_eligible": _yes_no(d.financial_need_eligible),
        "first_generation_student": _yes_no(d.first_generation_student),
    }
    facts |= {f"test_{k.lower()}": v for k, v in d.standardized_tests.items()}
    return {k: v for k, v in facts.items() if v}


# States in which an auto-staged application is still being worked on by the filler.
IN_FLIGHT = frozenset({S.QUEUED, S.PREPARING, S.FILLING, S.RESUME_QUEUED})
STAGE_POLL_S = 1.0
STAGE_TIMEOUT_S = 15 * 60.0

StageProgress = Callable[[int, int, str | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StageReport:
    started: list[uuid.UUID]
    states: dict[uuid.UUID, str]
    timed_out: bool = False

    def count(self, state: ApplicationState) -> int:
        return sum(1 for s in self.states.values() if s == state.value)


@dataclass(slots=True)
class AutoStageCampaign:
    """Campaign auto-stage: start filling the campaign's closest-deadline queued applications
    (BatchPrepare) and wait until each has left the filler - AWAITING_FINAL_VERIFICATION,
    PAUSED_NEEDS_USER_INPUT or BLOCKED_NEEDS_HUMAN - so the campaign only completes after its
    staging pass. Never submits. `progress(done, total, current_title)` is awaited on every
    change and may raise to cancel (the applications themselves keep running)."""

    uow_factory: IUnitOfWorkFactory
    batch: BatchPrepare
    poll_s: float = STAGE_POLL_S
    timeout_s: float = STAGE_TIMEOUT_S

    async def execute(
        self,
        user_id: uuid.UUID,
        campaign_id: uuid.UUID,
        limit: int,
        progress: StageProgress | None = None,
        already: Sequence[uuid.UUID] = (),
    ) -> StageReport:
        """`already`: applications the campaign started while it was still crawling (streaming
        auto-apply); they count against `limit` and are waited for as well."""
        started = list(dict.fromkeys(already))
        remaining = limit - len(started)
        if remaining > 0:
            started += (
                await self.batch.execute(
                    user_id, remaining, campaign_id=campaign_id, exclude=frozenset(started)
                )
            ).started
        states: dict[uuid.UUID, str] = dict.fromkeys(started, S.QUEUED.value)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        last: tuple[int, str | None] | None = None
        while True:
            titles: dict[uuid.UUID, str] = {}
            async with self.uow_factory(user_id) as uow:
                for app_id in started:
                    found = await uow.app_details.get(user_id, app_id)
                    if found is not None:
                        states[app_id] = found[0].state.value
                        titles[app_id] = found[1].title
            active = [a for a in started if S(states[a]) in IN_FLIGHT]
            done = len(started) - len(active)
            current = next(
                (titles.get(a) for a in active if states[a] != S.QUEUED.value),
                titles.get(active[0]) if active else None,
            )
            if progress is not None and (done, current) != last:
                last = (done, current)
                await progress(done, len(started), current)
            if not active:
                return StageReport(started, states)
            if loop.time() > deadline:
                return StageReport(started, states, timed_out=True)
            await asyncio.sleep(self.poll_s)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    application_id: uuid.UUID
    remaining_questions: int
    resumed: bool
    memory_qa_id: uuid.UUID | None


@dataclass(slots=True)
class AnswerQuestion:
    """Store the answer on the field (USER), teach the semantic memory, and resume once every
    open question of the application is answered."""

    deps: ApplicationDeps
    runner: IApplicationRunner

    async def execute(
        self,
        user_id: uuid.UUID,
        question_id: uuid.UUID,
        answer: str,
        *,
        save_to_memory: bool = True,
    ) -> AnswerResult:
        answer = answer.strip()
        if not answer:
            raise ValidationError("answer is empty")
        async with self.deps.uow_factory(user_id) as uow:
            question = await uow.questions.get(user_id, question_id)
            if question is None:
                raise NotFoundError("question not found")
            if question.status is not QuestionStatus.OPEN:
                raise ConflictError("this question was already answered")
            fields = {
                s.field.field_key: s.field
                for s in await uow.fields.for_application(user_id, question.application_id)
            }
        qa_id: uuid.UUID | None = None
        if save_to_memory:
            try:
                qa_id = (
                    await self.deps.learn.execute(
                        user_id, question=question.question_text, answer=answer
                    )
                ).id
            except ConflictError:
                async with self.deps.uow_factory(user_id) as uow:
                    existing = await uow.memory.find_question(
                        user_id, normalize_question(question.question_text)
                    )
                if existing is not None:
                    qa_id = (
                        await self.deps.update_memory.execute(user_id, existing.id, answer=answer)
                    ).id
        now = self.deps.clock.now()
        async with self.deps.uow_factory(user_id) as uow:
            row = fields.get(question.field_key)
            row_id = row.id if row else new_uuid7()
            await uow.fields.upsert(
                StoredField(
                    ApplicationField(
                        id=row_id,
                        user_id=user_id,
                        application_id=question.application_id,
                        step_index=question.step_index,
                        field_key=question.field_key,
                        label=question.question_text,
                        field_type=question.field_type,
                        options=question.options,
                        required=True,
                        value=None,
                        source=FieldSource.USER,
                        confidence=1.0,
                        memory_qa_id=qa_id,
                        document_id=None,
                        essay_draft_id=None,
                        user_edited=True,
                        updated_at=now,
                    ),
                    self.deps.cipher.encrypt(answer.encode(), _field_aad(user_id, row_id)),
                )
            )
            await uow.questions.mark_answered(user_id, question.id, qa_id, now)
            remaining = [
                q
                for q in await uow.questions.for_application(user_id, question.application_id)
                if q.status is QuestionStatus.OPEN and q.id != question.id
            ]
            resumed = False
            found = await uow.app_details.get(user_id, question.application_id)
            if found is not None and not remaining and found[0].state is S.PAUSED_NEEDS_USER_INPUT:
                await _transition(
                    uow,
                    user_id,
                    found[0],
                    S.RESUME_QUEUED,
                    actor=Actor.USER,
                    reason="all questions answered",
                    at=now,
                    title=found[1].title,
                )
                resumed = True
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="inbox.answered",
                    application_id=question.application_id,
                    payload={
                        "question_id": str(question.id),
                        "remaining": len(remaining),
                        "learned": qa_id is not None,
                    },
                )
            )
            await uow.commit()
        if resumed:
            await self.runner.start(user_id, question.application_id)
        return AnswerResult(question.application_id, len(remaining), resumed, qa_id)


@dataclass(frozen=True, slots=True)
class ApplicationDetail:
    application: Application
    opportunity: Opportunity
    transitions: list[Transition]
    fields: list[ApplicationField]
    questions: list[PendingQuestion]
    drafts: list[EssayDraft]
    checkpoints: list[Checkpoint]


@dataclass(slots=True)
class GetApplicationDetail:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> ApplicationDetail:
        async with self.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            transitions = await uow.app_details.transitions(user_id, application_id)
            stored = await uow.fields.for_application(user_id, application_id)
            questions = await uow.questions.for_application(user_id, application_id)
            drafts = await uow.essay_drafts.list(user_id, application_id)
            checkpoints = [
                c.checkpoint for c in await uow.checkpoints.list(user_id, application_id)
            ]
        fields: list[ApplicationField] = []
        for s in stored:
            f = s.field
            if s.value is not None:
                f.value = self.cipher.decrypt(s.value, _field_aad(user_id, f.id)).decode()
            fields.append(f)
        return ApplicationDetail(
            found[0], found[1], transitions, fields, questions, drafts, checkpoints
        )


@dataclass(frozen=True, slots=True)
class InboxItem:
    question: PendingQuestion
    application: Application
    opportunity: Opportunity


@dataclass(frozen=True, slots=True)
class Inbox:
    questions: list[InboxItem]
    awaiting_verification: list[tuple[Application, Opportunity]]
    blocked: list[tuple[Application, Opportunity]]


@dataclass(slots=True)
class GetInbox:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> Inbox:
        async with self.uow_factory(user_id) as uow:
            questions = await uow.questions.open_for_user(user_id)
            apps: dict[uuid.UUID, tuple[Application, Opportunity]] = {}
            for q in questions:
                if q.application_id not in apps:
                    found = await uow.app_details.get(user_id, q.application_id)
                    if found is not None:
                        apps[q.application_id] = found
            awaiting = await uow.app_details.list_by_states(
                user_id, [S.AWAITING_FINAL_VERIFICATION]
            )
            blocked = await uow.app_details.list_by_states(user_id, [S.BLOCKED_NEEDS_HUMAN])
        items = [
            InboxItem(q, *apps[q.application_id]) for q in questions if q.application_id in apps
        ]
        return Inbox(items, awaiting, blocked)


@dataclass(slots=True)
class GetCheckpointScreenshot:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    storage: IObjectStorage

    async def execute(self, user_id: uuid.UUID, checkpoint_id: uuid.UUID) -> bytes:
        async with self.uow_factory(user_id) as uow:
            stored = await uow.checkpoints.get(user_id, checkpoint_id)
        if stored is None or stored.screenshot_key is None or stored.screenshot_nonce is None:
            raise NotFoundError("screenshot not found")
        data = await self.storage.get(stored.screenshot_key)
        return self.cipher.decrypt(
            EncryptedSecret(data, stored.screenshot_nonce, stored.screenshot_key_version or 1),
            aad(CHECKPOINT_TABLE, "screenshot", user_id, checkpoint_id),
        )
