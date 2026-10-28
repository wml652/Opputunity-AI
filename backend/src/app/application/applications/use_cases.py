"""Application filling lifecycle: start -> fill -> pause for answers -> resume -> ready for OK.

Never submits: filling stops at the final step (AWAITING_FINAL_VERIFICATION); submission needs
the explicit human OK gate (Milestone 5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.application.essay.synthesis import EssayRequest, SynthesizeEssay
from app.application.memory.use_cases import LearnAnswer, RecallMemory, UpdateMemory
from app.application.notifications.use_cases import NotifyUser
from app.application.profile.use_cases import DownloadDocument, GetProfile
from app.application.services.field_mapper import DocumentRef, FieldMapper
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
    ResumeState,
)
from app.domain.applications.models import Application, Transition, advance
from app.domain.discovery.models import Opportunity
from app.domain.essay.synthesis import EssayDraft
from app.domain.events.models import NewEvent
from app.domain.llm.provider import EncryptedSecret
from app.domain.memory.models import normalize_question
from app.domain.notification.models import Template
from app.domain.ports.applications import (
    IApplicationRunner,
    IFormFiller,
    StoredCheckpoint,
    StoredField,
)
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.uow import IUnitOfWork, IUnitOfWorkFactory
from app.domain.ports.vault import IObjectStorage
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor, ApplicationState, QuestionStatus
from app.domain.shared.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

S = ApplicationState
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
            elif app.state is not S.FILLING:
                return None
            await uow.commit()
        if not opp.portal_url:
            await self._finish_blocked(
                user_id, app, opp, "the opportunity has no application portal URL"
            )
            return FillOutcomeKind.BLOCKED
        try:
            outcome = await self._fill(user_id, app, opp)
        except DomainError as exc:
            await self._finish_blocked(user_id, app, opp, exc.message)
            return FillOutcomeKind.BLOCKED
        except Exception as exc:  # noqa: BLE001 - the run must always leave a visible state
            await self._finish_blocked(user_id, app, opp, f"{type(exc).__name__}: {str(exc)[:300]}")
            return FillOutcomeKind.ERROR
        await self._finish(user_id, app, opp, outcome)
        return outcome.kind

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

        mapper = FieldMapper(
            facts=facts,
            documents=[DocumentRef(d.id, d.kind, d.filename) for d in documents],
            recall=recall,
            essay=essay,
            known=known,
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

        resume = None
        if checkpoint is not None:
            state = self.deps.cipher.decrypt(
                checkpoint.storage_state,
                aad(CHECKPOINT_TABLE, "storage_state", user_id, checkpoint.checkpoint.id),
            ).decode()
            resume = ResumeState(
                url=checkpoint.checkpoint.url,
                step_index=checkpoint.checkpoint.step_index,
                dom_fingerprint=checkpoint.checkpoint.dom_fingerprint,
                storage_state=state,
            )
        assert opp.portal_url is not None
        return await self.deps.filler.run(
            opp.portal_url, resume=resume, resolver=resolver, documents=blobs
        )

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
