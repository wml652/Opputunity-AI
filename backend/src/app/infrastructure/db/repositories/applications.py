"""Repositories for application filling, the Action Inbox, essay drafts and notifications."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.leases import PENDING_OWNER
from app.domain.applications.filling import (
    ApplicationField,
    Checkpoint,
    FieldSource,
    PendingQuestion,
)
from app.domain.applications.models import Application, Transition
from app.domain.applications.verification import FinalVerification, VerificationOutcome
from app.domain.discovery.models import Opportunity
from app.domain.essay.synthesis import EssayDraft
from app.domain.llm.provider import EncryptedSecret
from app.domain.notification.models import Notification, NotificationChannel, Template
from app.domain.ports.applications import (
    StoredChannel,
    StoredCheckpoint,
    StoredField,
    StoredReceipt,
)
from app.domain.shared.enums import ApplicationState, NotifStatus, QuestionStatus
from app.infrastructure.db.models import (
    ApplicationCheckpointRow,
    ApplicationFieldRow,
    ApplicationRow,
    ApplicationTransitionRow,
    EssayDraftRow,
    FinalVerificationRow,
    NotificationChannelRow,
    NotificationRow,
    OpportunityRow,
    PendingQuestionRow,
)
from app.infrastructure.db.repositories.discovery import _app, _opp


def _secret(ct: bytes | None, nonce: bytes | None, version: int | None) -> EncryptedSecret | None:
    if ct is None or nonce is None or version is None:
        return None
    return EncryptedSecret(ciphertext=ct, nonce=nonce, key_version=version)


# ---- application detail -------------------------------------------------------------------------


class SqlApplicationDetailRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> tuple[Application, Opportunity] | None:
        row = (
            await self._s.execute(
                select(ApplicationRow, OpportunityRow)
                .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
                .where(ApplicationRow.user_id == user_id, ApplicationRow.id == application_id)
            )
        ).first()
        return (_app(row[0]), _opp(row[1])) if row else None

    async def save_state(
        self,
        user_id: uuid.UUID,
        app: Application,
        expected_version: int,
        last_error: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "state": app.state,
            "version": app.version,
            "updated_at": app.updated_at,
        }
        if last_error is not None:
            values["last_error"] = last_error[:2000]
        if app.state is ApplicationState.SUBMITTED:
            values["submitted_at"] = app.updated_at
        result = await self._s.execute(
            update(ApplicationRow)
            .where(
                ApplicationRow.user_id == user_id,
                ApplicationRow.id == app.id,
                ApplicationRow.version == expected_version,
            )
            .values(**values)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def transitions(self, user_id: uuid.UUID, application_id: uuid.UUID) -> list[Transition]:
        rows = await self._s.scalars(
            select(ApplicationTransitionRow)
            .where(
                ApplicationTransitionRow.user_id == user_id,
                ApplicationTransitionRow.application_id == application_id,
            )
            .order_by(ApplicationTransitionRow.at)
        )
        return [
            Transition(r.application_id, r.from_state, r.to_state, r.actor, r.reason, r.at)
            for r in rows
        ]

    async def list_by_states(
        self, user_id: uuid.UUID, states: Sequence[ApplicationState]
    ) -> list[tuple[Application, Opportunity]]:
        rows = (
            await self._s.execute(
                select(ApplicationRow, OpportunityRow)
                .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
                .where(ApplicationRow.user_id == user_id, ApplicationRow.state.in_(list(states)))
                .order_by(ApplicationRow.deadline_at.asc().nulls_last())
            )
        ).all()
        return [(_app(a), _opp(o)) for a, o in rows]

    async def set_lease(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        owner: str | None,
        expires_at: datetime | None,
    ) -> None:
        await self._s.execute(
            update(ApplicationRow)
            .where(ApplicationRow.user_id == user_id, ApplicationRow.id == application_id)
            .values(lock_owner=owner, lease_expires_at=expires_at)
        )

    async def past_deadline(
        self, now: datetime, states: Sequence[ApplicationState]
    ) -> list[tuple[Application, Opportunity]]:
        rows = (
            await self._s.execute(
                select(ApplicationRow, OpportunityRow)
                .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
                .where(
                    ApplicationRow.state.in_(list(states)),
                    ApplicationRow.deadline_at.is_not(None),
                    ApplicationRow.deadline_at < now,
                )
                .limit(500)
            )
        ).all()
        return [(_app(a), _opp(o)) for a, o in rows]

    async def claim_lease(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = await self._s.execute(
            update(ApplicationRow)
            .where(
                ApplicationRow.user_id == user_id,
                ApplicationRow.id == application_id,
                or_(
                    ApplicationRow.lock_owner.is_(None),
                    ApplicationRow.lock_owner == owner,
                    ApplicationRow.lock_owner == PENDING_OWNER,
                    ApplicationRow.lease_expires_at.is_(None),
                    ApplicationRow.lease_expires_at < now,
                ),
            )
            .values(lock_owner=owner, lease_expires_at=expires_at)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def mark_resumed(
        self, user_id: uuid.UUID, application_id: uuid.UUID, pending_until: datetime
    ) -> None:
        await self._s.execute(
            update(ApplicationRow)
            .where(ApplicationRow.user_id == user_id, ApplicationRow.id == application_id)
            .values(
                resume_count=ApplicationRow.resume_count + 1,
                lock_owner=PENDING_OWNER,
                lease_expires_at=pending_until,
            )
        )

    async def stale_leases(
        self,
        now: datetime,
        states: Sequence[ApplicationState],
        *,
        spare_pending_after: datetime | None = None,
    ) -> list[tuple[Application, Opportunity]]:
        query = (
            select(ApplicationRow, OpportunityRow)
            .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
            .where(
                ApplicationRow.state.in_(list(states)),
                ApplicationRow.lease_expires_at.is_not(None),
                ApplicationRow.lease_expires_at < now,
            )
        )
        if spare_pending_after is not None:
            query = query.where(
                or_(
                    ApplicationRow.lock_owner.is_(None),
                    ApplicationRow.lock_owner != PENDING_OWNER,
                    ApplicationRow.lease_expires_at <= spare_pending_after,
                )
            )
        rows = (await self._s.execute(query.limit(500))).all()
        return [(_app(a), _opp(o)) for a, o in rows]


# ---- final verification -------------------------------------------------------------------------


def _verification(r: FinalVerificationRow) -> FinalVerification:
    return FinalVerification(
        id=r.id,
        user_id=r.user_id,
        application_id=r.application_id,
        checkpoint_id=r.checkpoint_id,
        preview_hash_seen=r.preview_hash_seen,
        approved_preview_hash=r.approved_preview_hash,
        edited_fields=list(r.edited_fields or []),
        outcome=VerificationOutcome(r.outcome),
        created_at=r.created_at,
        approved_at=r.approved_at,
        rejected_at=r.rejected_at,
        reject_reason=r.reject_reason,
        submitted_at=r.submitted_at,
        observed_hash=r.observed_hash,
        final_url=r.final_url,
        error=r.error,
        mismatches=list(r.mismatches or []),
        has_receipt=r.receipt_key is not None,
    )


class SqlFinalVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def _values(v: FinalVerification) -> dict[str, Any]:
        return {
            "checkpoint_id": v.checkpoint_id,
            "outcome": v.outcome.value,
            "preview_hash_seen": v.preview_hash_seen,
            "approved_preview_hash": v.approved_preview_hash,
            "observed_hash": v.observed_hash,
            "edited_fields": v.edited_fields,
            "mismatches": v.mismatches,
            "approved_at": v.approved_at,
            "rejected_at": v.rejected_at,
            "reject_reason": v.reject_reason,
            "submitted_at": v.submitted_at,
            "final_url": v.final_url,
            "error": v.error,
        }

    async def add(self, verification: FinalVerification) -> None:
        self._s.add(
            FinalVerificationRow(
                id=verification.id,
                user_id=verification.user_id,
                application_id=verification.application_id,
                created_at=verification.created_at,
                **self._values(verification),
            )
        )
        await self._s.flush()

    async def update(
        self, verification: FinalVerification, receipt: StoredReceipt | None = None
    ) -> None:
        values = self._values(verification)
        if receipt is not None:
            text = receipt.text
            values |= {
                "receipt_key": receipt.screenshot_key,
                "receipt_nonce": receipt.screenshot_nonce,
                "receipt_key_version": receipt.screenshot_key_version,
                "receipt_text_ciphertext": text.ciphertext if text else None,
                "receipt_text_nonce": text.nonce if text else None,
                "receipt_text_key_version": text.key_version if text else None,
            }
        await self._s.execute(
            update(FinalVerificationRow)
            .where(
                FinalVerificationRow.user_id == verification.user_id,
                FinalVerificationRow.id == verification.id,
            )
            .values(**values)
        )

    async def latest(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> FinalVerification | None:
        row = await self._s.scalar(
            select(FinalVerificationRow)
            .where(
                FinalVerificationRow.user_id == user_id,
                FinalVerificationRow.application_id == application_id,
            )
            .order_by(FinalVerificationRow.created_at.desc(), FinalVerificationRow.id.desc())
            .limit(1)
        )
        return _verification(row) if row else None

    async def receipt(self, user_id: uuid.UUID, verification_id: uuid.UUID) -> StoredReceipt | None:
        row = await self._s.scalar(
            select(FinalVerificationRow).where(
                FinalVerificationRow.user_id == user_id,
                FinalVerificationRow.id == verification_id,
            )
        )
        if row is None:
            return None
        text = None
        if (
            row.receipt_text_ciphertext is not None
            and row.receipt_text_nonce is not None
            and row.receipt_text_key_version is not None
        ):
            text = EncryptedSecret(
                ciphertext=row.receipt_text_ciphertext,
                nonce=row.receipt_text_nonce,
                key_version=row.receipt_text_key_version,
            )
        return StoredReceipt(row.receipt_key, row.receipt_nonce, row.receipt_key_version, text)


# ---- fields -------------------------------------------------------------------------------------


class SqlFieldRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(self, stored: StoredField) -> None:
        f, v = stored.field, stored.value
        values: dict[str, Any] = {
            "step_index": f.step_index,
            "label": f.label[:2000],
            "field_type": f.field_type,
            "options": f.options[:200],
            "required": f.required,
            "value_ciphertext": v.ciphertext if v else None,
            "value_nonce": v.nonce if v else None,
            "value_key_version": v.key_version if v else None,
            "value_source": f.source.value,
            "confidence": f.confidence,
            "memory_qa_id": f.memory_qa_id,
            "document_id": f.document_id,
            "essay_draft_id": f.essay_draft_id,
            "user_edited": f.user_edited,
            "updated_at": f.updated_at,
            "evidence": (f.evidence or "")[:1000] or None,
        }
        stmt = pg_insert(ApplicationFieldRow).values(
            id=f.id,
            user_id=f.user_id,
            application_id=f.application_id,
            field_key=f.field_key,
            **values,
        )
        await self._s.execute(
            stmt.on_conflict_do_update(constraint="uq_application_fields_key", set_=values)
        )

    async def for_application(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[StoredField]:
        rows = await self._s.scalars(
            select(ApplicationFieldRow)
            .where(
                ApplicationFieldRow.user_id == user_id,
                ApplicationFieldRow.application_id == application_id,
            )
            .order_by(ApplicationFieldRow.step_index, ApplicationFieldRow.field_key)
        )
        return [
            StoredField(
                field=ApplicationField(
                    id=r.id,
                    user_id=r.user_id,
                    application_id=r.application_id,
                    step_index=r.step_index,
                    field_key=r.field_key,
                    label=r.label,
                    field_type=r.field_type,
                    options=list(r.options or []),
                    required=r.required,
                    value=None,
                    source=FieldSource(r.value_source),
                    confidence=r.confidence,
                    memory_qa_id=r.memory_qa_id,
                    document_id=r.document_id,
                    essay_draft_id=r.essay_draft_id,
                    user_edited=r.user_edited,
                    updated_at=r.updated_at,
                    evidence=r.evidence,
                ),
                value=_secret(r.value_ciphertext, r.value_nonce, r.value_key_version),
            )
            for r in rows
        ]


# ---- questions ----------------------------------------------------------------------------------


def _question(r: PendingQuestionRow) -> PendingQuestion:
    return PendingQuestion(
        id=r.id,
        user_id=r.user_id,
        application_id=r.application_id,
        field_key=r.field_key,
        step_index=r.step_index,
        question_text=r.question_text,
        context=r.context,
        field_type=r.field_type,
        options=list(r.options or []),
        suggested_answer=r.suggested_answer,
        confidence=r.confidence,
        status=r.status,
        answered_qa_id=r.answered_qa_id,
        created_at=r.created_at,
        answered_at=r.answered_at,
    )


class SqlQuestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, question: PendingQuestion) -> bool:
        q = question
        existing = await self._s.scalar(
            select(PendingQuestionRow.id).where(
                PendingQuestionRow.application_id == q.application_id,
                PendingQuestionRow.field_key == q.field_key,
                PendingQuestionRow.status == QuestionStatus.OPEN,
            )
        )
        if existing is not None:
            return False
        self._s.add(
            PendingQuestionRow(
                id=q.id,
                user_id=q.user_id,
                application_id=q.application_id,
                field_key=q.field_key,
                step_index=q.step_index,
                question_text=q.question_text[:4000],
                context=q.context[:4000],
                field_type=q.field_type,
                options=q.options[:200],
                suggested_answer=q.suggested_answer,
                confidence=q.confidence,
                status=q.status,
                created_at=q.created_at,
            )
        )
        await self._s.flush()
        return True

    async def get(self, user_id: uuid.UUID, question_id: uuid.UUID) -> PendingQuestion | None:
        row = await self._s.scalar(
            select(PendingQuestionRow).where(
                PendingQuestionRow.user_id == user_id, PendingQuestionRow.id == question_id
            )
        )
        return _question(row) if row else None

    async def open_for_user(self, user_id: uuid.UUID) -> list[PendingQuestion]:
        rows = await self._s.scalars(
            select(PendingQuestionRow)
            .where(
                PendingQuestionRow.user_id == user_id,
                PendingQuestionRow.status == QuestionStatus.OPEN,
            )
            .order_by(PendingQuestionRow.created_at)
        )
        return [_question(r) for r in rows]

    async def for_application(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[PendingQuestion]:
        rows = await self._s.scalars(
            select(PendingQuestionRow)
            .where(
                PendingQuestionRow.user_id == user_id,
                PendingQuestionRow.application_id == application_id,
            )
            .order_by(PendingQuestionRow.created_at)
        )
        return [_question(r) for r in rows]

    async def mark_answered(
        self, user_id: uuid.UUID, question_id: uuid.UUID, qa_id: uuid.UUID | None, at: datetime
    ) -> None:
        await self._s.execute(
            update(PendingQuestionRow)
            .where(PendingQuestionRow.user_id == user_id, PendingQuestionRow.id == question_id)
            .values(status=QuestionStatus.ANSWERED, answered_qa_id=qa_id, answered_at=at)
        )


# ---- checkpoints --------------------------------------------------------------------------------


def _checkpoint(r: ApplicationCheckpointRow) -> StoredCheckpoint:
    return StoredCheckpoint(
        checkpoint=Checkpoint(
            id=r.id,
            user_id=r.user_id,
            application_id=r.application_id,
            seq=r.seq,
            step_index=r.step_index,
            url=r.url,
            dom_fingerprint=r.dom_fingerprint,
            fill_ledger=list(r.fill_ledger or []),
            has_screenshot=r.screenshot_key is not None,
            created_at=r.created_at,
        ),
        storage_state=EncryptedSecret(
            r.storage_state_ciphertext, r.storage_state_nonce, r.storage_state_key_version
        ),
        screenshot_key=r.screenshot_key,
        screenshot_nonce=r.screenshot_nonce,
        screenshot_key_version=r.screenshot_key_version,
    )


class SqlCheckpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, stored: StoredCheckpoint) -> None:
        c, st = stored.checkpoint, stored.storage_state
        self._s.add(
            ApplicationCheckpointRow(
                id=c.id,
                user_id=c.user_id,
                application_id=c.application_id,
                seq=c.seq,
                step_index=c.step_index,
                url=c.url[:2000],
                dom_fingerprint=c.dom_fingerprint,
                storage_state_ciphertext=st.ciphertext,
                storage_state_nonce=st.nonce,
                storage_state_key_version=st.key_version,
                fill_ledger=c.fill_ledger,
                screenshot_key=stored.screenshot_key,
                screenshot_nonce=stored.screenshot_nonce,
                screenshot_key_version=stored.screenshot_key_version,
                created_at=c.created_at,
            )
        )
        await self._s.flush()

    async def latest(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> StoredCheckpoint | None:
        row = await self._s.scalar(
            select(ApplicationCheckpointRow)
            .where(
                ApplicationCheckpointRow.user_id == user_id,
                ApplicationCheckpointRow.application_id == application_id,
            )
            .order_by(ApplicationCheckpointRow.seq.desc())
            .limit(1)
        )
        return _checkpoint(row) if row else None

    async def list(self, user_id: uuid.UUID, application_id: uuid.UUID) -> list[StoredCheckpoint]:
        rows = await self._s.scalars(
            select(ApplicationCheckpointRow)
            .where(
                ApplicationCheckpointRow.user_id == user_id,
                ApplicationCheckpointRow.application_id == application_id,
            )
            .order_by(ApplicationCheckpointRow.seq)
        )
        return [_checkpoint(r) for r in rows]

    async def get(self, user_id: uuid.UUID, checkpoint_id: uuid.UUID) -> StoredCheckpoint | None:
        row = await self._s.scalar(
            select(ApplicationCheckpointRow).where(
                ApplicationCheckpointRow.user_id == user_id,
                ApplicationCheckpointRow.id == checkpoint_id,
            )
        )
        return _checkpoint(row) if row else None

    async def next_seq(self, user_id: uuid.UUID, application_id: uuid.UUID) -> int:
        current = await self._s.scalar(
            select(func.max(ApplicationCheckpointRow.seq)).where(
                ApplicationCheckpointRow.user_id == user_id,
                ApplicationCheckpointRow.application_id == application_id,
            )
        )
        return int(current or 0) + 1


# ---- essay drafts -------------------------------------------------------------------------------


def _draft(r: EssayDraftRow) -> EssayDraft:
    return EssayDraft(
        id=r.id,
        user_id=r.user_id,
        application_id=r.application_id,
        opportunity_id=r.opportunity_id,
        field_key=r.field_key,
        prompt=r.prompt,
        word_limit=r.word_limit,
        body=r.body,
        critique=dict(r.critique),
        voice_match_score=r.voice_match_score,
        burstiness_score=r.burstiness_score,
        status=r.status,
        requires_manual=r.requires_manual,
        model=r.model,
        fingerprint_version=r.fingerprint_version,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


class SqlEssayDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def _values(self, d: EssayDraft) -> dict[str, Any]:
        return {
            "application_id": d.application_id,
            "opportunity_id": d.opportunity_id,
            "field_key": d.field_key,
            "prompt": d.prompt,
            "word_limit": d.word_limit,
            "body": d.body,
            "critique": d.critique,
            "voice_match_score": d.voice_match_score,
            "burstiness_score": d.burstiness_score,
            "status": d.status,
            "requires_manual": d.requires_manual,
            "model": d.model,
            "fingerprint_version": d.fingerprint_version,
            "updated_at": d.updated_at,
        }

    async def add(self, draft: EssayDraft) -> None:
        self._s.add(
            EssayDraftRow(
                id=draft.id,
                user_id=draft.user_id,
                created_at=draft.created_at,
                **self._values(draft),
            )
        )
        await self._s.flush()

    async def get(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> EssayDraft | None:
        row = await self._s.scalar(
            select(EssayDraftRow).where(
                EssayDraftRow.user_id == user_id, EssayDraftRow.id == draft_id
            )
        )
        return _draft(row) if row else None

    async def list(self, user_id: uuid.UUID, application_id: uuid.UUID | None) -> list[EssayDraft]:
        query = select(EssayDraftRow).where(EssayDraftRow.user_id == user_id)
        if application_id is not None:
            query = query.where(EssayDraftRow.application_id == application_id)
        rows = await self._s.scalars(query.order_by(EssayDraftRow.created_at.desc()).limit(100))
        return [_draft(r) for r in rows]

    async def update(self, draft: EssayDraft) -> None:
        await self._s.execute(
            update(EssayDraftRow)
            .where(EssayDraftRow.user_id == draft.user_id, EssayDraftRow.id == draft.id)
            .values(**self._values(draft))
        )

    async def for_field(
        self, user_id: uuid.UUID, application_id: uuid.UUID, field_key: str
    ) -> EssayDraft | None:
        row = await self._s.scalar(
            select(EssayDraftRow)
            .where(
                EssayDraftRow.user_id == user_id,
                EssayDraftRow.application_id == application_id,
                EssayDraftRow.field_key == field_key,
            )
            .order_by(EssayDraftRow.created_at.desc())
            .limit(1)
        )
        return _draft(row) if row else None


# ---- notifications ------------------------------------------------------------------------------


class SqlChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def _stored(r: NotificationChannelRow) -> StoredChannel:
        return StoredChannel(
            channel=NotificationChannel(
                id=r.id,
                user_id=r.user_id,
                channel=r.channel,
                address="",
                address_hint=r.address_hint,
                enabled=r.enabled,
                verified_at=r.verified_at,
                created_at=r.created_at,
            ),
            address=EncryptedSecret(r.address_ciphertext, r.address_nonce, r.address_key_version),
            verify_code_hash=r.verify_code_hash,
            verify_expires_at=r.verify_expires_at,
        )

    async def list(self, user_id: uuid.UUID) -> list[StoredChannel]:
        rows = await self._s.scalars(
            select(NotificationChannelRow)
            .where(NotificationChannelRow.user_id == user_id)
            .order_by(NotificationChannelRow.created_at)
        )
        return [self._stored(r) for r in rows]

    async def get(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> StoredChannel | None:
        row = await self._s.scalar(
            select(NotificationChannelRow).where(
                NotificationChannelRow.user_id == user_id, NotificationChannelRow.id == channel_id
            )
        )
        return self._stored(row) if row else None

    async def save(self, stored: StoredChannel) -> None:
        c, a = stored.channel, stored.address
        values = {
            "address_ciphertext": a.ciphertext,
            "address_nonce": a.nonce,
            "address_key_version": a.key_version,
            "address_hint": c.address_hint,
            "enabled": c.enabled,
            "verified_at": c.verified_at,
            "verify_code_hash": stored.verify_code_hash,
            "verify_expires_at": stored.verify_expires_at,
        }
        stmt = pg_insert(NotificationChannelRow).values(
            id=c.id, user_id=c.user_id, channel=c.channel, created_at=c.created_at, **values
        )
        await self._s.execute(
            stmt.on_conflict_do_update(
                constraint="uq_notification_channels_user_channel", set_=values
            )
        )

    async def delete(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(NotificationChannelRow).where(
                NotificationChannelRow.user_id == user_id, NotificationChannelRow.id == channel_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


class SqlNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_if_new(self, notification: Notification) -> bool:
        n = notification
        inserted = await self._s.scalar(
            pg_insert(NotificationRow)
            .values(
                id=n.id,
                user_id=n.user_id,
                channel=n.channel,
                template=n.template.value,
                dedupe_key=n.dedupe_key,
                payload=n.payload,
                status=n.status,
                provider=n.provider,
                provider_message_id=n.provider_message_id,
                error=n.error,
                related_application_id=n.related_application_id,
                created_at=n.created_at,
                updated_at=n.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_notifications_dedupe")
            .returning(NotificationRow.id)
        )
        return inserted is not None

    async def update_result(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        status: NotifStatus,
        provider: str | None,
        provider_message_id: str | None,
        error: str | None,
    ) -> None:
        await self._s.execute(
            update(NotificationRow)
            .where(NotificationRow.user_id == user_id, NotificationRow.id == notification_id)
            .values(
                status=status,
                provider=provider,
                provider_message_id=provider_message_id,
                error=(error or "")[:1000] or None,
                updated_at=func.now(),
            )
        )

    async def update_by_provider_id(
        self, provider_message_id: str, status: NotifStatus, error: str | None
    ) -> int:
        result = await self._s.execute(
            update(NotificationRow)
            .where(NotificationRow.provider_message_id == provider_message_id)
            .values(status=status, error=(error or "")[:1000] or None, updated_at=func.now())
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def recent(self, user_id: uuid.UUID, limit: int) -> list[Notification]:
        rows = await self._s.scalars(
            select(NotificationRow)
            .where(NotificationRow.user_id == user_id)
            .order_by(NotificationRow.created_at.desc())
            .limit(limit)
        )
        return [
            Notification(
                id=r.id,
                user_id=r.user_id,
                channel=r.channel,
                template=Template(r.template),
                dedupe_key=r.dedupe_key,
                payload=dict(r.payload),
                status=r.status,
                provider=r.provider,
                provider_message_id=r.provider_message_id,
                error=r.error,
                related_application_id=r.related_application_id,
                created_at=r.created_at,
            )
            for r in rows
        ]
