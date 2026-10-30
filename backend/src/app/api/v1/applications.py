"""Action Inbox, application inspector, essay drafts, notifications, provider webhooks."""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import Container, CurrentUserId
from app.api.v1.discovery import OpportunityOut
from app.application.essay.synthesis import EssayRequest
from app.domain.applications.filling import ApplicationField, Checkpoint, PendingQuestion
from app.domain.applications.models import Application, Transition
from app.domain.applications.verification import FinalVerification
from app.domain.essay.synthesis import EssayDraft
from app.domain.notification.models import Notification, NotificationChannel
from app.domain.notification.signatures import verify_meta_signature, verify_twilio_signature
from app.domain.shared.enums import NotifChannel, NotifStatus
from app.domain.shared.errors import AuthenticationError

router = APIRouter(tags=["applications"])

# ---- schemas ------------------------------------------------------------------------------------


class TransitionOut(BaseModel):
    from_state: str | None
    to_state: str
    actor: str
    reason: str
    at: datetime

    @staticmethod
    def of(t: Transition) -> TransitionOut:
        return TransitionOut(
            from_state=t.from_state.value if t.from_state else None,
            to_state=t.to_state.value,
            actor=t.actor.value,
            reason=t.reason,
            at=t.at,
        )


class FieldOut(BaseModel):
    field_key: str
    step_index: int
    label: str
    field_type: str
    options: list[str]
    required: bool
    value: str | None
    source: str
    confidence: float
    essay_draft_id: uuid.UUID | None
    user_edited: bool
    evidence: str | None = None

    @staticmethod
    def of(f: ApplicationField) -> FieldOut:
        return FieldOut(
            field_key=f.field_key,
            step_index=f.step_index,
            label=f.label,
            field_type=f.field_type,
            options=f.options,
            required=f.required,
            value=f.value,
            source=f.source.value,
            confidence=round(f.confidence, 4),
            essay_draft_id=f.essay_draft_id,
            user_edited=f.user_edited,
            evidence=f.evidence,
        )


class QuestionOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    field_key: str
    step_index: int
    question: str
    context: str
    field_type: str
    options: list[str]
    suggested_answer: str | None
    confidence: float
    status: str
    created_at: datetime
    answered_at: datetime | None

    @staticmethod
    def of(q: PendingQuestion) -> QuestionOut:
        return QuestionOut(
            id=q.id,
            application_id=q.application_id,
            field_key=q.field_key,
            step_index=q.step_index,
            question=q.question_text,
            context=q.context,
            field_type=q.field_type,
            options=q.options,
            suggested_answer=q.suggested_answer,
            confidence=round(q.confidence, 4),
            status=q.status.value,
            created_at=q.created_at,
            answered_at=q.answered_at,
        )


class DraftOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID | None
    field_key: str | None
    prompt: str
    word_limit: int | None
    body: str
    critique: dict[str, Any]
    voice_match_score: float | None
    burstiness_score: float | None
    status: str
    requires_manual: bool
    model: str | None
    created_at: datetime | None

    @staticmethod
    def of(d: EssayDraft) -> DraftOut:
        return DraftOut(
            id=d.id,
            application_id=d.application_id,
            field_key=d.field_key,
            prompt=d.prompt,
            word_limit=d.word_limit,
            body=d.body,
            critique=d.critique,
            voice_match_score=d.voice_match_score,
            burstiness_score=d.burstiness_score,
            status=d.status.value,
            requires_manual=d.requires_manual,
            model=d.model,
            created_at=d.created_at,
        )


class CheckpointOut(BaseModel):
    id: uuid.UUID
    seq: int
    step_index: int
    url: str
    has_screenshot: bool
    fields_filled: int
    created_at: datetime

    @staticmethod
    def of(c: Checkpoint) -> CheckpointOut:
        return CheckpointOut(
            id=c.id,
            seq=c.seq,
            step_index=c.step_index,
            url=c.url,
            has_screenshot=c.has_screenshot,
            fields_filled=len(c.fill_ledger),
            created_at=c.created_at,
        )


class ApplicationOut(BaseModel):
    id: uuid.UUID
    state: str
    priority_rank: int | None
    deadline_at: datetime | None
    version: int

    @staticmethod
    def of(a: Application) -> ApplicationOut:
        return ApplicationOut(
            id=a.id,
            state=a.state.value,
            priority_rank=a.priority_rank,
            deadline_at=a.deadline_at,
            version=a.version,
        )


class ApplicationDetailOut(BaseModel):
    application: ApplicationOut
    opportunity: OpportunityOut
    transitions: list[TransitionOut]
    fields: list[FieldOut]
    questions: list[QuestionOut]
    drafts: list[DraftOut]
    checkpoints: list[CheckpointOut]


class InboxQuestionOut(QuestionOut):
    opportunity_title: str
    organization: str | None
    portal_url: str | None


class InboxAppOut(BaseModel):
    application: ApplicationOut
    opportunity: OpportunityOut


class InboxOut(BaseModel):
    questions: list[InboxQuestionOut]
    awaiting_verification: list[InboxAppOut]
    blocked: list[InboxAppOut]


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=10_000)
    save_to_memory: bool = True


class AnswerOut(BaseModel):
    application_id: uuid.UUID
    remaining_questions: int
    resumed: bool
    memory_qa_id: uuid.UUID | None


class DraftIn(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)
    word_limit: int | None = Field(default=None, ge=50, le=2000)


class DraftEditIn(BaseModel):
    body: str = Field(min_length=1, max_length=30_000)


class ChannelIn(BaseModel):
    channel: NotifChannel
    address: str = Field(min_length=3, max_length=320)


class VerifyIn(BaseModel):
    code: str = Field(min_length=4, max_length=10)


class ChannelOut(BaseModel):
    id: uuid.UUID
    channel: NotifChannel
    address_hint: str
    enabled: bool
    verified: bool
    created_at: datetime

    @staticmethod
    def of(c: NotificationChannel) -> ChannelOut:
        return ChannelOut(
            id=c.id,
            channel=c.channel,
            address_hint=c.address_hint,
            enabled=c.enabled,
            verified=c.verified_at is not None,
            created_at=c.created_at,
        )


class NotificationOut(BaseModel):
    channel: NotifChannel
    template: str
    status: NotifStatus
    provider: str | None
    error: str | None
    created_at: datetime

    @staticmethod
    def of(n: Notification) -> NotificationOut:
        return NotificationOut(
            channel=n.channel,
            template=n.template.value,
            status=n.status,
            provider=n.provider,
            error=n.error,
            created_at=n.created_at,
        )


class NotificationSettingsOut(BaseModel):
    channels: list[ChannelOut]
    providers: dict[str, str]
    recent: list[NotificationOut]


class DeliveryOut(BaseModel):
    channel: NotifChannel
    status: NotifStatus
    provider: str | None
    error: str | None


# ---- inbox & applications -----------------------------------------------------------------------


@router.get("/inbox", response_model=InboxOut)
async def inbox(user_id: CurrentUserId, container: Container) -> InboxOut:
    data = await container.applications.inbox.execute(user_id)
    return InboxOut(
        questions=[
            InboxQuestionOut(
                **QuestionOut.of(i.question).model_dump(),
                opportunity_title=i.opportunity.title,
                organization=i.opportunity.organization,
                portal_url=i.opportunity.portal_url,
            )
            for i in data.questions
        ],
        awaiting_verification=[
            InboxAppOut(application=ApplicationOut.of(a), opportunity=OpportunityOut.of(o))
            for a, o in data.awaiting_verification
        ],
        blocked=[
            InboxAppOut(application=ApplicationOut.of(a), opportunity=OpportunityOut.of(o))
            for a, o in data.blocked
        ],
    )


@router.post("/inbox/questions/{question_id}/answer", response_model=AnswerOut)
async def answer_question(
    question_id: uuid.UUID, body: AnswerIn, user_id: CurrentUserId, container: Container
) -> AnswerOut:
    result = await container.applications.answer.execute(
        user_id, question_id, body.answer, save_to_memory=body.save_to_memory
    )
    return AnswerOut(
        application_id=result.application_id,
        remaining_questions=result.remaining_questions,
        resumed=result.resumed,
        memory_qa_id=result.memory_qa_id,
    )


@router.get("/applications/{application_id}", response_model=ApplicationDetailOut)
async def application_detail(
    application_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> ApplicationDetailOut:
    d = await container.applications.detail.execute(user_id, application_id)
    return ApplicationDetailOut(
        application=ApplicationOut.of(d.application),
        opportunity=OpportunityOut.of(d.opportunity),
        transitions=[TransitionOut.of(t) for t in d.transitions],
        fields=[FieldOut.of(f) for f in d.fields],
        questions=[QuestionOut.of(q) for q in d.questions],
        drafts=[DraftOut.of(x) for x in d.drafts],
        checkpoints=[CheckpointOut.of(c) for c in d.checkpoints],
    )


@router.post("/applications/{application_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_filling(
    application_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> dict[str, str]:
    await container.applications.start.execute(user_id, application_id)
    return {"status": "started"}


@router.get("/applications/checkpoints/{checkpoint_id}/screenshot")
async def checkpoint_screenshot(
    checkpoint_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    png = await container.applications.screenshot.execute(user_id, checkpoint_id)
    return Response(
        content=png, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


# ---- final verification gate (M5) ---------------------------------------------------------------


class VerificationRecordOut(BaseModel):
    id: uuid.UUID
    outcome: str
    preview_hash_seen: str
    approved_preview_hash: str | None
    observed_hash: str | None
    edited_fields: list[str]
    mismatches: list[dict[str, str]]
    approved_at: datetime | None
    rejected_at: datetime | None
    reject_reason: str | None
    submitted_at: datetime | None
    final_url: str | None
    error: str | None
    has_receipt: bool
    created_at: datetime

    @staticmethod
    def of(v: FinalVerification) -> VerificationRecordOut:
        return VerificationRecordOut(
            id=v.id,
            outcome=v.outcome.value,
            preview_hash_seen=v.preview_hash_seen,
            approved_preview_hash=v.approved_preview_hash,
            observed_hash=v.observed_hash,
            edited_fields=v.edited_fields,
            mismatches=v.mismatches,
            approved_at=v.approved_at,
            rejected_at=v.rejected_at,
            reject_reason=v.reject_reason,
            submitted_at=v.submitted_at,
            final_url=v.final_url,
            error=v.error,
            has_receipt=v.has_receipt,
            created_at=v.created_at,
        )


class VerificationOut(BaseModel):
    application: ApplicationOut
    opportunity: OpportunityOut
    fields: list[FieldOut]
    drafts: list[DraftOut]
    checkpoint: CheckpointOut | None
    preview_hash: str
    latest: VerificationRecordOut | None
    receipt_text: str | None


class ApproveIn(BaseModel):
    preview_hash: str = Field(min_length=64, max_length=64)
    edits: dict[str, str] = Field(default_factory=dict, max_length=200)


class RejectIn(BaseModel):
    preview_hash: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=500)


class BatchIn(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)


class BatchOut(BaseModel):
    started: list[uuid.UUID]
    skipped_no_portal: int


@router.post(
    "/applications/batch-prepare", response_model=BatchOut, status_code=status.HTTP_202_ACCEPTED
)
async def batch_prepare(body: BatchIn, user_id: CurrentUserId, container: Container) -> BatchOut:
    """Fill the top-N queued applications up to the final OK gate. Never submits."""
    result = await container.applications.batch.execute(user_id, body.limit)
    return BatchOut(started=result.started, skipped_no_portal=result.skipped_no_portal)


class UnblockOut(BaseModel):
    application_id: uuid.UUID
    domain: str
    url: str


@router.post(
    "/applications/{application_id}/unblock",
    response_model=UnblockOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def unblock(
    application_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> UnblockOut:
    """ "Sign In in Browser & Resume": a visible browser opens at the portal; once the user has
    signed in, the cookies are saved (encrypted) and the application resumes with them."""
    started = await container.applications.unblock.execute(user_id, application_id)
    return UnblockOut(application_id=started.application_id, domain=started.domain, url=started.url)


@router.get("/applications/{application_id}/verification", response_model=VerificationOut)
async def verification(
    application_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> VerificationOut:
    v = await container.applications.verification.execute(user_id, application_id)
    return VerificationOut(
        application=ApplicationOut.of(v.application),
        opportunity=OpportunityOut.of(v.opportunity),
        fields=[FieldOut.of(f) for f in v.fields],
        drafts=[DraftOut.of(d) for d in v.drafts],
        checkpoint=CheckpointOut.of(v.checkpoint) if v.checkpoint else None,
        preview_hash=v.preview_hash,
        latest=VerificationRecordOut.of(v.latest) if v.latest else None,
        receipt_text=v.receipt_text,
    )


@router.post(
    "/applications/{application_id}/approve",
    response_model=VerificationRecordOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve(
    application_id: uuid.UUID, body: ApproveIn, user_id: CurrentUserId, container: Container
) -> VerificationRecordOut:
    record = await container.applications.approve.execute(
        user_id, application_id, body.preview_hash, body.edits
    )
    return VerificationRecordOut.of(record)


@router.post("/applications/{application_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject(
    application_id: uuid.UUID, body: RejectIn, user_id: CurrentUserId, container: Container
) -> Response:
    await container.applications.reject.execute(
        user_id, application_id, body.preview_hash, body.reason
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/applications/{application_id}/receipt")
async def receipt(
    application_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    png = await container.applications.receipt.execute(user_id, application_id)
    return Response(
        content=png, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


# ---- essay drafts -------------------------------------------------------------------------------


@router.get("/essays/drafts", response_model=list[DraftOut])
async def list_drafts(
    user_id: CurrentUserId,
    container: Container,
    application_id: uuid.UUID | None = Query(default=None),
) -> list[DraftOut]:
    return [
        DraftOut.of(d)
        for d in await container.applications.list_drafts.execute(user_id, application_id)
    ]


@router.post("/essays/drafts", response_model=DraftOut, status_code=status.HTTP_201_CREATED)
async def create_draft(body: DraftIn, user_id: CurrentUserId, container: Container) -> DraftOut:
    draft = await container.applications.synthesize.execute(
        user_id, EssayRequest(prompt=body.prompt, word_limit=body.word_limit)
    )
    return DraftOut.of(draft)


@router.put("/essays/drafts/{draft_id}", response_model=DraftOut)
async def edit_draft(
    draft_id: uuid.UUID, body: DraftEditIn, user_id: CurrentUserId, container: Container
) -> DraftOut:
    return DraftOut.of(
        await container.applications.edit_draft.execute(user_id, draft_id, body.body)
    )


# ---- notifications ------------------------------------------------------------------------------


@router.get("/notifications", response_model=NotificationSettingsOut)
async def notification_settings(
    user_id: CurrentUserId, container: Container
) -> NotificationSettingsOut:
    n = container.notifications
    return NotificationSettingsOut(
        channels=[ChannelOut.of(c) for c in await n.list_channels.execute(user_id)],
        providers=n.providers,
        recent=[NotificationOut.of(x) for x in await n.recent.execute(user_id)],
    )


@router.post(
    "/notifications/channels", response_model=ChannelOut, status_code=status.HTTP_201_CREATED
)
async def save_channel(body: ChannelIn, user_id: CurrentUserId, container: Container) -> ChannelOut:
    return ChannelOut.of(
        await container.notifications.save_channel.execute(user_id, body.channel, body.address)
    )


@router.post("/notifications/channels/{channel_id}/verify", response_model=ChannelOut)
async def verify_channel(
    channel_id: uuid.UUID, body: VerifyIn, user_id: CurrentUserId, container: Container
) -> ChannelOut:
    return ChannelOut.of(
        await container.notifications.verify_channel.execute(user_id, channel_id, body.code)
    )


@router.delete("/notifications/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    await container.notifications.delete_channel.execute(user_id, channel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notifications/test", response_model=list[DeliveryOut])
async def test_notification(user_id: CurrentUserId, container: Container) -> list[DeliveryOut]:
    results = await container.notifications.send_test.execute(user_id)
    return [
        DeliveryOut(channel=r.channel, status=r.status, provider=r.provider, error=r.error)
        for r in results
    ]


# ---- provider webhooks (no session auth: verified by signature) ---------------------------------

_TWILIO_STATUS = {
    "delivered": NotifStatus.DELIVERED,
    "read": NotifStatus.DELIVERED,
    "sent": NotifStatus.SENT,
    "failed": NotifStatus.FAILED,
    "undelivered": NotifStatus.FAILED,
}


@router.post("/webhooks/twilio", status_code=status.HTTP_204_NO_CONTENT)
async def twilio_webhook(
    request: Request, container: Container, x_twilio_signature: str = Header(default="")
) -> Response:
    token = container.notifications.twilio_auth_token
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = container.notifications.public_api_url.rstrip("/") + "/api/v1/webhooks/twilio"
    if not token or not verify_twilio_signature(token, url, form, x_twilio_signature):
        raise AuthenticationError("invalid webhook signature")
    sid, raw = form.get("MessageSid"), form.get("MessageStatus", "")
    if sid and raw in _TWILIO_STATUS:
        await container.notifications.receipt.execute(
            sid, _TWILIO_STATUS[raw], form.get("ErrorMessage")
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/webhooks/meta")
async def meta_verify(
    container: Container,
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    expected = container.notifications.meta_verify_token
    if mode != "subscribe" or not expected or not hmac.compare_digest(expected, token):
        raise AuthenticationError("verification failed")
    return Response(content=challenge, media_type="text/plain")


@router.post("/webhooks/meta", status_code=status.HTTP_204_NO_CONTENT)
async def meta_webhook(
    request: Request, container: Container, x_hub_signature_256: str = Header(default="")
) -> Response:
    secret = container.notifications.meta_app_secret
    body = await request.body()
    if not secret or not verify_meta_signature(secret, body, x_hub_signature_256):
        raise AuthenticationError("invalid webhook signature")
    data = json.loads(body or b"{}")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for st in (change.get("value") or {}).get("statuses", []):
                mapped = _TWILIO_STATUS.get(str(st.get("status")))
                if st.get("id") and mapped:
                    await container.notifications.receipt.execute(str(st["id"]), mapped, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
