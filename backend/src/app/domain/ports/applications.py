"""Ports for application filling, the Action Inbox, essay drafts and notifications."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.applications.filling import (
    ApplicationField,
    Checkpoint,
    DocumentBlob,
    FieldResolution,
    FillOutcome,
    FormField,
    PendingQuestion,
    ResumeState,
)
from app.domain.applications.models import Application, Transition
from app.domain.applications.verification import FinalVerification, SubmitOutcome
from app.domain.discovery.models import Opportunity
from app.domain.essay.synthesis import EssayDraft
from app.domain.llm.provider import EncryptedSecret
from app.domain.notification.models import Notification, NotificationChannel, RenderedMessage
from app.domain.shared.enums import ApplicationState, NotifChannel, NotifStatus

Resolver = Callable[[int, list[FormField]], Awaitable[list[FieldResolution]]]


class IFormFiller(Protocol):
    async def run(
        self,
        portal_url: str,
        *,
        resume: ResumeState | None,
        resolver: Resolver,
        documents: dict[uuid.UUID, DocumentBlob],
    ) -> FillOutcome:
        """Fill step by step up to (never clicking) the final Submit."""
        ...

    async def submit(
        self,
        portal_url: str,
        *,
        approved: dict[str, FieldResolution],
        approved_hash: str,
        storage_state: str | None,
        documents: dict[uuid.UUID, DocumentBlob],
    ) -> SubmitOutcome:
        """Replay every step with the APPROVED values, read each control back from the page and
        click Submit only if the hash of the observed values equals `approved_hash`."""
        ...


class IApplicationRunner(Protocol):
    async def start(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None: ...

    async def submit(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        """Run the approved submission in the background (one task per application)."""
        ...


class INotifier(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def channel(self) -> NotifChannel: ...

    async def send(self, address: str, message: RenderedMessage) -> str:
        """Returns the provider message id; raises NotificationError on failure."""
        ...


# ---- repositories -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredField:
    field: ApplicationField  # `value` is None here; the secret carries it
    value: EncryptedSecret | None


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    checkpoint: Checkpoint
    storage_state: EncryptedSecret
    screenshot_key: str | None
    screenshot_nonce: bytes | None
    screenshot_key_version: int | None


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    screenshot_key: str | None
    screenshot_nonce: bytes | None
    screenshot_key_version: int | None
    text: EncryptedSecret | None


class IFinalVerificationRepository(Protocol):
    async def add(self, verification: FinalVerification) -> None: ...
    async def update(
        self, verification: FinalVerification, receipt: StoredReceipt | None = None
    ) -> None: ...
    async def latest(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> FinalVerification | None: ...
    async def receipt(
        self, user_id: uuid.UUID, verification_id: uuid.UUID
    ) -> StoredReceipt | None: ...


class IApplicationDetailRepository(Protocol):
    async def get(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> tuple[Application, Opportunity] | None: ...

    async def save_state(
        self,
        user_id: uuid.UUID,
        app: Application,
        expected_version: int,
        last_error: str | None = None,
    ) -> bool:
        """Optimistic update; False when another writer changed the row first."""
        ...

    async def set_lease(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        owner: str | None,
        expires_at: datetime | None,
    ) -> None:
        """Claim (or release, with None) the worker lease of a running application."""
        ...

    async def past_deadline(
        self, now: datetime, states: Sequence[ApplicationState]
    ) -> list[tuple[Application, Opportunity]]:
        """System scope: applications in `states` whose deadline has passed."""
        ...

    async def stale_leases(
        self, now: datetime, states: Sequence[ApplicationState]
    ) -> list[tuple[Application, Opportunity]]:
        """System scope: applications in `states` whose worker lease expired."""
        ...

    async def transitions(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[Transition]: ...

    async def list_by_states(
        self, user_id: uuid.UUID, states: Sequence[ApplicationState]
    ) -> list[tuple[Application, Opportunity]]: ...


class IFieldRepository(Protocol):
    async def upsert(self, stored: StoredField) -> None: ...

    async def for_application(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[StoredField]: ...


class IQuestionRepository(Protocol):
    async def add(self, question: PendingQuestion) -> bool:
        """False if an OPEN question already exists for this field."""
        ...

    async def get(self, user_id: uuid.UUID, question_id: uuid.UUID) -> PendingQuestion | None: ...

    async def open_for_user(self, user_id: uuid.UUID) -> list[PendingQuestion]: ...

    async def for_application(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[PendingQuestion]: ...

    async def mark_answered(
        self, user_id: uuid.UUID, question_id: uuid.UUID, qa_id: uuid.UUID | None, at: datetime
    ) -> None: ...


class ICheckpointRepository(Protocol):
    async def add(self, stored: StoredCheckpoint) -> None: ...

    async def latest(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> StoredCheckpoint | None: ...

    async def list(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[StoredCheckpoint]: ...

    async def get(
        self, user_id: uuid.UUID, checkpoint_id: uuid.UUID
    ) -> StoredCheckpoint | None: ...

    async def next_seq(self, user_id: uuid.UUID, application_id: uuid.UUID) -> int: ...


class IEssayDraftRepository(Protocol):
    async def add(self, draft: EssayDraft) -> None: ...

    async def get(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> EssayDraft | None: ...

    async def list(
        self, user_id: uuid.UUID, application_id: uuid.UUID | None
    ) -> list[EssayDraft]: ...

    async def update(self, draft: EssayDraft) -> None: ...

    async def for_field(
        self, user_id: uuid.UUID, application_id: uuid.UUID, field_key: str
    ) -> EssayDraft | None: ...


@dataclass(frozen=True, slots=True)
class StoredChannel:
    channel: NotificationChannel  # `address` is "" here; the secret carries it
    address: EncryptedSecret
    verify_code_hash: str | None
    verify_expires_at: datetime | None


class IChannelRepository(Protocol):
    async def list(self, user_id: uuid.UUID) -> list[StoredChannel]: ...

    async def get(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> StoredChannel | None: ...

    async def save(self, stored: StoredChannel) -> None: ...

    async def delete(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> bool: ...


class INotificationRepository(Protocol):
    async def add_if_new(self, notification: Notification) -> bool:
        """False when (user, channel, dedupe_key) was already sent."""
        ...

    async def update_result(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        status: NotifStatus,
        provider: str | None,
        provider_message_id: str | None,
        error: str | None,
    ) -> None: ...

    async def update_by_provider_id(
        self, provider_message_id: str, status: NotifStatus, error: str | None
    ) -> int:
        """System scope (webhooks): delivery receipts carry no tenant."""
        ...

    async def recent(self, user_id: uuid.UUID, limit: int) -> list[Notification]: ...
