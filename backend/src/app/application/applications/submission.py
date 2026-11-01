"""Milestone 5: the final verification gate, submission, receipts, deadline sweep, lease reaper.

Invariant: NOTHING is submitted without a recorded human OK whose `preview_hash` matches.

* `GetVerification` shows exactly what will be sent (screenshot + every field) and its hash.
* `ApproveSubmission` rejects a stale preview (the values changed since the user looked),
  applies the user's edits, records a `final_verifications` row bound to the APPROVED hash and
  only then moves the application to SUBMIT_APPROVED.
* `RunSubmission` replays the portal with the approved values and clicks Submit only if the page
  shows exactly those values (observed hash == approved hash). A mismatch re-stages the
  application for review; a failure is never retried automatically (it might have gone through).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.application.applications.live import live_observer
from app.application.applications.portal_sessions import (
    combine_with_checkpoint,
    saved_portal_state,
)
from app.application.applications.use_cases import (
    CHECKPOINT_TABLE,
    ApplicationDeps,
    _field_aad,
    _transition,
)
from app.domain.agent.models import TeachStep, detect_ats, workflow_domain
from app.domain.applications.filling import (
    ApplicationField,
    Checkpoint,
    DocumentBlob,
    FieldResolution,
    FieldSource,
)
from app.domain.applications.models import TERMINAL, Application
from app.domain.applications.verification import (
    FinalVerification,
    PreviewValue,
    SubmitOutcome,
    SubmitOutcomeKind,
    VerificationOutcome,
    compute_preview_hash,
)
from app.domain.discovery.models import Opportunity
from app.domain.essay.synthesis import EssayDraft
from app.domain.events.models import NewEvent
from app.domain.llm.provider import EncryptedSecret
from app.domain.notification.models import Template
from app.domain.ports.applications import StoredField, StoredReceipt
from app.domain.ports.crypto import aad
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor, ApplicationState
from app.domain.shared.errors import ConflictError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

S = ApplicationState
VERIFICATION_TABLE = "final_verifications"
LEASE = timedelta(minutes=30)
LEASE_OWNER = "in-process"


def _preview_values(fields: list[ApplicationField]) -> list[PreviewValue]:
    return [PreviewValue(f.step_index, f.field_key, f.value) for f in fields]


@dataclass(frozen=True, slots=True)
class VerificationPreview:
    application: Application
    opportunity: Opportunity
    fields: list[ApplicationField]
    drafts: list[EssayDraft]
    checkpoint: Checkpoint | None
    preview_hash: str
    latest: FinalVerification | None
    receipt_text: str | None


async def _load_fields(
    deps: ApplicationDeps, user_id: uuid.UUID, application_id: uuid.UUID
) -> list[ApplicationField]:
    async with deps.uow_factory(user_id) as uow:
        stored = await uow.fields.for_application(user_id, application_id)
    fields: list[ApplicationField] = []
    for s in stored:
        f = s.field
        if s.value is not None:
            f.value = deps.cipher.decrypt(s.value, _field_aad(user_id, f.id)).decode()
        fields.append(f)
    return sorted(fields, key=lambda f: (f.step_index, f.field_key))


@dataclass(slots=True)
class GetVerification:
    deps: ApplicationDeps

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> VerificationPreview:
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            drafts = await uow.essay_drafts.list(user_id, application_id)
            checkpoint = await uow.checkpoints.latest(user_id, application_id)
            latest = await uow.verifications.latest(user_id, application_id)
            receipt = await uow.verifications.receipt(user_id, latest.id) if latest else None
        fields = await _load_fields(self.deps, user_id, application_id)
        receipt_text = None
        if latest is not None and receipt is not None and receipt.text is not None:
            receipt_text = self.deps.cipher.decrypt(
                receipt.text, aad(VERIFICATION_TABLE, "receipt_text", user_id, latest.id)
            ).decode()
        return VerificationPreview(
            application=found[0],
            opportunity=found[1],
            fields=fields,
            drafts=drafts,
            checkpoint=checkpoint.checkpoint if checkpoint else None,
            preview_hash=compute_preview_hash(_preview_values(fields)),
            latest=latest,
            receipt_text=receipt_text,
        )


@dataclass(slots=True)
class ApproveSubmission:
    """The human "OK - Verify & Submit"."""

    deps: ApplicationDeps
    start_submission: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

    async def execute(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        preview_hash: str,
        edits: dict[str, str],
    ) -> FinalVerification:
        fields = await _load_fields(self.deps, user_id, application_id)
        by_key = {f.field_key: f for f in fields}
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            app, opp = found
            if app.state is not S.AWAITING_FINAL_VERIFICATION:
                raise ConflictError(
                    f"nothing to approve: the application is {app.state.value.lower()}"
                )
            current = compute_preview_hash(_preview_values(fields))
            if preview_hash != current:
                raise ConflictError(
                    "the application changed since you reviewed it: reload the preview and "
                    "check it again before approving"
                )
            unknown = [k for k in edits if k not in by_key]
            if unknown:
                raise ValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
            for key, value in edits.items():
                if by_key[key].field_type == "file":
                    raise ValidationError(f"{key}: replace documents in the Vault, not here")
                if by_key[key].required and not value.strip():
                    raise ValidationError(f"{key} is required")
            drafts = await uow.essay_drafts.list(user_id, application_id)
            for draft in drafts:
                if (
                    draft.requires_manual
                    and draft.field_key
                    and draft.field_key in by_key
                    and draft.field_key not in edits
                    and not by_key[draft.field_key].user_edited
                ):
                    raise ValidationError(
                        f"{draft.field_key}: this portal prohibits AI-written essays; write or "
                        "rewrite it yourself before approving"
                    )
            now = self.deps.clock.now()
            edited: list[str] = []
            for key, value in edits.items():
                f = by_key[key]
                if (f.value or "") == value:
                    continue
                edited.append(key)
                f.value = value
                updated = replace(
                    f,
                    value=None,
                    source=FieldSource.USER,
                    confidence=1.0,
                    user_edited=True,
                    updated_at=now,
                )
                await uow.fields.upsert(
                    StoredField(
                        updated,
                        self.deps.cipher.encrypt(value.encode(), _field_aad(user_id, f.id)),
                    )
                )
            checkpoint = await uow.checkpoints.latest(user_id, application_id)
            verification = FinalVerification(
                id=new_uuid7(),
                user_id=user_id,
                application_id=application_id,
                checkpoint_id=checkpoint.checkpoint.id if checkpoint else None,
                preview_hash_seen=preview_hash,
                approved_preview_hash=compute_preview_hash(_preview_values(fields)),
                edited_fields=sorted(edited),
                outcome=VerificationOutcome.APPROVED,
                created_at=now,
                approved_at=now,
            )
            await uow.verifications.add(verification)
            await _transition(
                uow,
                user_id,
                app,
                S.SUBMIT_APPROVED,
                actor=Actor.USER,
                reason=(
                    f"approved by you ({len(edited)} edit(s))" if edited else "approved by you"
                ),
                at=now,
                title=opp.title,
            )
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="application.submit_approved",
                entity="applications",
                entity_id=application_id,
                metadata={
                    "preview_hash": preview_hash,
                    "approved_hash": verification.approved_preview_hash,
                    "edited_fields": sorted(edited),
                },
            )
            await uow.commit()
        await self.start_submission(user_id, application_id)
        return verification


@dataclass(slots=True)
class RejectApplication:
    """The human "Reject / Cancel": nothing is submitted, the application is cancelled."""

    uow_factory: IUnitOfWorkFactory
    clock: Clock

    async def execute(
        self, user_id: uuid.UUID, application_id: uuid.UUID, preview_hash: str, reason: str
    ) -> None:
        async with self.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            app, opp = found
            if app.state is not S.AWAITING_FINAL_VERIFICATION:
                raise ConflictError(f"cannot reject: the application is {app.state.value.lower()}")
            now = self.clock.now()
            await uow.verifications.add(
                FinalVerification(
                    id=new_uuid7(),
                    user_id=user_id,
                    application_id=application_id,
                    checkpoint_id=None,
                    preview_hash_seen=preview_hash or "-",
                    approved_preview_hash=None,
                    edited_fields=[],
                    outcome=VerificationOutcome.REJECTED,
                    created_at=now,
                    rejected_at=now,
                    reject_reason=(reason or "rejected").strip()[:500],
                )
            )
            await _transition(
                uow,
                user_id,
                app,
                S.CANCELLED,
                actor=Actor.USER,
                reason=f"rejected by you: {(reason or 'no reason given').strip()[:160]}",
                at=now,
                title=opp.title,
            )
            await uow.commit()


@dataclass(slots=True)
class RunSubmission:
    """Worker side of the OK gate. Idempotent: a second run after SUBMITTED is a no-op, and an
    interrupted SUBMITTING is never retried blindly (the lease reaper hands it to the user)."""

    deps: ApplicationDeps

    async def _learned_path(self, user_id: uuid.UUID, portal_url: str) -> list[TeachStep]:
        async with self.deps.uow_factory(user_id) as uow:
            workflow = await uow.agent_workflows.find(
                user_id, workflow_domain(portal_url), detect_ats(portal_url)
            )
        return workflow.navigation_steps if workflow else []

    async def execute(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> SubmitOutcomeKind | None:
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            if found is None:
                raise NotFoundError("application not found")
            app, opp = found
            verification = await uow.verifications.latest(user_id, application_id)
            if app.state is not S.SUBMIT_APPROVED:
                return None  # already submitted / not approved: never submit twice
            if (
                verification is None
                or verification.outcome is not VerificationOutcome.APPROVED
                or not verification.approved_preview_hash
            ):
                raise ConflictError("no recorded human OK for this application")
            now = self.deps.clock.now()
            await _transition(
                uow,
                user_id,
                app,
                S.SUBMITTING,
                actor=Actor.WORKER,
                reason="submitting the approved values",
                at=now,
                title=opp.title,
            )
            await uow.app_details.set_lease(user_id, app.id, LEASE_OWNER, now + LEASE)
            checkpoint = await uow.checkpoints.latest(user_id, application_id)
            documents = await uow.documents.list(user_id)
            await uow.commit()
        fields = await _load_fields(self.deps, user_id, application_id)
        approved_hash = compute_preview_hash(_preview_values(fields))
        if approved_hash != verification.approved_preview_hash:
            outcome = SubmitOutcome(
                SubmitOutcomeKind.MISMATCH,
                error="stored values changed after the approval",
            )
        elif not opp.portal_url:
            outcome = SubmitOutcome(SubmitOutcomeKind.ERROR, error="no portal URL")
        else:
            session_state, session_at = await saved_portal_state(
                self.deps.uow_factory,
                self.deps.cipher,
                self.deps.clock.now(),
                user_id,
                opp.portal_url,
                *([checkpoint.checkpoint.url] if checkpoint is not None else []),
            )
            storage_state = session_state
            if checkpoint is not None:
                storage_state = combine_with_checkpoint(
                    self.deps.cipher.decrypt(
                        checkpoint.storage_state,
                        aad(CHECKPOINT_TABLE, "storage_state", user_id, checkpoint.checkpoint.id),
                    ).decode(),
                    checkpoint.checkpoint.created_at,
                    session_state,
                    session_at,
                )
            blobs: dict[uuid.UUID, DocumentBlob] = {}
            for doc in documents:
                _, data = await self.deps.download.execute(user_id, doc.id)
                blobs[doc.id] = DocumentBlob(doc.id, doc.filename, data)
            approved = {
                f.field_key: FieldResolution(
                    f.field_key,
                    f.value,
                    f.source,
                    1.0,
                    document_id=f.document_id,
                    note="approved",
                )
                for f in fields
                if f.value is not None
            }
            try:
                outcome = await self.deps.filler.submit(
                    opp.portal_url,
                    approved=approved,
                    approved_hash=approved_hash,
                    storage_state=storage_state,
                    documents=blobs,
                    observer=live_observer(
                        self.deps.uow_factory,
                        self.deps.live_frames,
                        self.deps.clock,
                        user_id,
                        application_id,
                        opp.title,
                        "SUBMITTING",
                    ),
                    learned_path=await self._learned_path(user_id, opp.portal_url),
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never retried automatically
                outcome = SubmitOutcome(
                    SubmitOutcomeKind.ERROR, error=f"{type(exc).__name__}: {str(exc)[:300]}"
                )
        await self._record(user_id, app.id, opp, verification, outcome)
        return outcome.kind

    async def _record(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        opp: Opportunity,
        verification: FinalVerification,
        outcome: SubmitOutcome,
    ) -> None:
        now = self.deps.clock.now()
        receipt: StoredReceipt | None = None
        if outcome.kind is SubmitOutcomeKind.SUBMITTED:
            shot_key = shot_nonce = shot_version = None
            if outcome.receipt_png:
                shot = self.deps.cipher.encrypt(
                    outcome.receipt_png,
                    aad(VERIFICATION_TABLE, "receipt", user_id, verification.id),
                )
                shot_key = f"{user_id}/receipts/{verification.id}.bin"
                await self.deps.storage.put(shot_key, shot.ciphertext)
                shot_nonce, shot_version = shot.nonce, shot.key_version
            text = (
                self.deps.cipher.encrypt(
                    outcome.receipt_text.encode(),
                    aad(VERIFICATION_TABLE, "receipt_text", user_id, verification.id),
                )
                if outcome.receipt_text
                else None
            )
            receipt = StoredReceipt(shot_key, shot_nonce, shot_version, text)
        verification.observed_hash = outcome.observed_hash
        verification.mismatches = outcome.mismatches
        verification.final_url = outcome.final_url
        verification.error = outcome.error
        if outcome.kind is SubmitOutcomeKind.SUBMITTED:
            verification.outcome, verification.submitted_at = VerificationOutcome.SUBMITTED, now
            target, reason, template = S.SUBMITTED, "submitted; receipt saved", Template.SUBMITTED
        elif outcome.kind is SubmitOutcomeKind.MISMATCH:
            verification.outcome = VerificationOutcome.RESTAGED
            keys = ", ".join(m.get("field", "?") for m in outcome.mismatches[:5])
            target = S.AWAITING_FINAL_VERIFICATION
            reason = (
                f"not submitted: the page showed different values ({keys}); review again"
                if keys
                else f"not submitted: {outcome.error or 'values differ'}; review again"
            )
            template = Template.READY_FOR_OK
        else:
            verification.outcome = VerificationOutcome.FAILED
            target = S.SUBMISSION_FAILED
            reason = f"submission failed: {outcome.error or outcome.kind.value.lower()}"
            template = Template.FAILED
        async with self.deps.uow_factory(user_id) as uow:
            found = await uow.app_details.get(user_id, application_id)
            assert found is not None
            app = found[0]
            await uow.verifications.update(verification, receipt)
            if app.state is S.SUBMITTING:
                await _transition(
                    uow,
                    user_id,
                    app,
                    target,
                    actor=Actor.WORKER,
                    reason=reason[:300],
                    at=now,
                    error=outcome.error if target is not S.SUBMITTED else None,
                    title=opp.title,
                )
            await uow.app_details.set_lease(user_id, application_id, None, None)
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="application.submission",
                    application_id=application_id,
                    payload={
                        "application_id": str(application_id),
                        "title": opp.title,
                        "outcome": verification.outcome.value,
                        "mismatches": len(outcome.mismatches),
                    },
                )
            )
            await uow.commit()
        await self.deps.notify.execute(
            user_id,
            template,
            {"title": opp.title, "path": f"/applications/{application_id}"},
            application_id=application_id,
            state=f"{verification.outcome.value}:{verification.id}",
        )


@dataclass(slots=True)
class GetReceiptScreenshot:
    deps: ApplicationDeps

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> bytes:
        async with self.deps.uow_factory(user_id) as uow:
            latest = await uow.verifications.latest(user_id, application_id)
            receipt = await uow.verifications.receipt(user_id, latest.id) if latest else None
        if (
            latest is None
            or receipt is None
            or receipt.screenshot_key is None
            or receipt.screenshot_nonce is None
        ):
            raise NotFoundError("no receipt for this application")
        data = await self.deps.storage.get(receipt.screenshot_key)
        return self.deps.cipher.decrypt(
            EncryptedSecret(data, receipt.screenshot_nonce, receipt.screenshot_key_version or 1),
            aad(VERIFICATION_TABLE, "receipt", user_id, latest.id),
        )


# ---- maintenance (system scope) -----------------------------------------------------------------

# Never expire an application while its approved submission is in flight.
EXPIRABLE = [s for s in S if s not in TERMINAL and s not in (S.SUBMIT_APPROVED, S.SUBMITTING)]


@dataclass(slots=True)
class SweepDeadlines:
    """Expire non-terminal applications whose deadline has passed (paused/queued included)."""

    system_uow_factory: IUnitOfWorkFactory
    clock: Clock

    async def execute(self) -> int:
        now = self.clock.now()
        async with self.system_uow_factory() as uow:
            items = await uow.app_details.past_deadline(now, EXPIRABLE)
            for app, opp in items:
                await _transition(
                    uow,
                    app.user_id,
                    app,
                    S.EXPIRED,
                    actor=Actor.SYSTEM,
                    reason="deadline passed",
                    at=now,
                    title=opp.title,
                )
            await uow.commit()
        return len(items)


@dataclass(slots=True)
class ReapLeases:
    """A worker that died mid-run leaves its lease behind. Filling runs are handed back to the
    user (Retry); an interrupted SUBMITTING is NEVER retried automatically - it may already have
    been submitted - so it becomes SUBMISSION_FAILED with a "check the portal" note."""

    system_uow_factory: IUnitOfWorkFactory
    clock: Clock

    async def execute(self) -> int:
        now = self.clock.now()
        async with self.system_uow_factory() as uow:
            items = await uow.app_details.stale_leases(now, [S.PREPARING, S.FILLING, S.SUBMITTING])
            for app, opp in items:
                if app.state is S.SUBMITTING:
                    target = S.SUBMISSION_FAILED
                    reason = (
                        "interrupted while submitting: check the portal before retrying - it may "
                        "already have been submitted"
                    )
                else:
                    target = S.BLOCKED_NEEDS_HUMAN
                    reason = "interrupted (the worker stopped); press Retry to continue"
                await _transition(
                    uow,
                    app.user_id,
                    app,
                    target,
                    actor=Actor.SYSTEM,
                    reason=reason,
                    at=now,
                    error=reason,
                    title=opp.title,
                )
                await uow.app_details.set_lease(app.user_id, app.id, None, None)
            await uow.commit()
        return len(items)


def lease_deadline(now: datetime) -> datetime:
    return now + LEASE
