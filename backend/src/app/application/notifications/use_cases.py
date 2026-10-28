"""Multi-channel notifications: verified channels only, deduplicated per
(template, application, state), no PII and no one-tap actions in the message."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.domain.notification.models import (
    Notification,
    NotificationChannel,
    Template,
    dedupe_key,
    mask_address,
    render,
)
from app.domain.ports.applications import INotifier, StoredChannel
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import NotifChannel, NotifStatus
from app.domain.shared.errors import DomainError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

TABLE = "notification_channels"
CODE_TTL = timedelta(minutes=15)


class NotificationError(DomainError):
    code = "notification_failed"


@dataclass(frozen=True, slots=True)
class NotificationDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    clock: Clock
    notifiers: dict[NotifChannel, INotifier] = field(default_factory=dict)
    console_url: str = "http://localhost:3000"


def _aad(user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    return aad(TABLE, "address", user_id, row_id)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: NotifChannel
    status: NotifStatus
    provider: str | None
    error: str | None
    deduplicated: bool = False


@dataclass(slots=True)
class NotifyUser:
    deps: NotificationDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        template: Template,
        payload: dict[str, Any],
        *,
        application_id: uuid.UUID | None = None,
        state: str | None = None,
    ) -> list[DeliveryResult]:
        async with self.deps.uow_factory(user_id) as uow:
            channels = [
                c
                for c in await uow.channels.list(user_id)
                if c.channel.enabled and c.channel.verified_at is not None
            ]
        message = render(template, payload, self.deps.console_url)
        key = dedupe_key(template, application_id, state)
        results: list[DeliveryResult] = []
        for stored in channels:
            ch = stored.channel
            now = self.deps.clock.now()
            notification = Notification(
                id=new_uuid7(),
                user_id=user_id,
                channel=ch.channel,
                template=template,
                dedupe_key=key,
                payload={k: v for k, v in payload.items() if k in ("title", "questions", "path")},
                status=NotifStatus.QUEUED,
                provider=None,
                provider_message_id=None,
                error=None,
                related_application_id=application_id,
                created_at=now,
            )
            async with self.deps.uow_factory(user_id) as uow:
                fresh = await uow.notifications.add_if_new(notification)
                await uow.commit()
            if not fresh:
                results.append(
                    DeliveryResult(ch.channel, NotifStatus.SENT, None, None, deduplicated=True)
                )
                continue
            notifier = self.deps.notifiers.get(ch.channel)
            status, provider, message_id, error = NotifStatus.FAILED, None, None, None
            if notifier is None:
                error = f"no {ch.channel.value} provider configured"
            else:
                provider = notifier.name
                address = self.deps.cipher.decrypt(stored.address, _aad(user_id, ch.id)).decode()
                try:
                    message_id = await notifier.send(address, message)
                    status = NotifStatus.SENT
                except DomainError as exc:
                    error = exc.message
            async with self.deps.uow_factory(user_id) as uow:
                await uow.notifications.update_result(
                    user_id, notification.id, status, provider, message_id, error
                )
                await uow.commit()
            results.append(DeliveryResult(ch.channel, status, provider, error))
        return results


@dataclass(slots=True)
class SaveChannel:
    """Add/replace the user's EMAIL or WHATSAPP address and send a one-time verification code."""

    deps: NotificationDeps

    async def execute(
        self, user_id: uuid.UUID, channel: NotifChannel, address: str
    ) -> NotificationChannel:
        address = address.strip()
        if channel is NotifChannel.EMAIL and ("@" not in address or len(address) > 320):
            raise ValidationError("enter a valid email address")
        if channel is NotifChannel.WHATSAPP:
            digits = "".join(c for c in address if c.isdigit())
            if not address.startswith("+") or not 8 <= len(digits) <= 15:
                raise ValidationError(
                    "enter the WhatsApp number in international format, e.g. +923001234567"
                )
            address = "+" + digits
        async with self.deps.uow_factory(user_id) as uow:
            existing = next(
                (c for c in await uow.channels.list(user_id) if c.channel.channel is channel), None
            )
        row_id = existing.channel.id if existing else new_uuid7()
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = self.deps.clock.now()
        ch = NotificationChannel(
            id=row_id,
            user_id=user_id,
            channel=channel,
            address="",
            address_hint=mask_address(channel, address),
            enabled=True,
            verified_at=None,
            created_at=existing.channel.created_at if existing else now,
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.channels.save(
                StoredChannel(
                    ch,
                    self.deps.cipher.encrypt(address.encode(), _aad(user_id, row_id)),
                    _hash(code),
                    now + CODE_TTL,
                )
            )
            await uow.commit()
        notifier = self.deps.notifiers.get(channel)
        if notifier is None:
            raise ValidationError(f"no {channel.value} provider is configured on the server")
        try:
            await notifier.send(
                address, render(Template.CHANNEL_VERIFY, {"code": code}, self.deps.console_url)
            )
        except DomainError as exc:
            raise ValidationError(f"could not send the verification code: {exc.message}") from exc
        return ch


@dataclass(slots=True)
class VerifyChannel:
    deps: NotificationDeps

    async def execute(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, code: str
    ) -> NotificationChannel:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.channels.get(user_id, channel_id)
            if stored is None:
                raise NotFoundError("channel not found")
            now = self.deps.clock.now()
            if (
                stored.verify_code_hash is None
                or stored.verify_expires_at is None
                or stored.verify_expires_at < now
                or not hmac.compare_digest(stored.verify_code_hash, _hash(code.strip()))
            ):
                raise ValidationError("invalid or expired verification code")
            stored.channel.verified_at = now
            await uow.channels.save(StoredChannel(stored.channel, stored.address, None, None))
            await uow.commit()
        return stored.channel


@dataclass(slots=True)
class ListChannels:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> list[NotificationChannel]:
        async with self.uow_factory(user_id) as uow:
            return [s.channel for s in await uow.channels.list(user_id)]


@dataclass(slots=True)
class DeleteChannel:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> None:
        async with self.uow_factory(user_id) as uow:
            if not await uow.channels.delete(user_id, channel_id):
                raise NotFoundError("channel not found")
            await uow.commit()


@dataclass(slots=True)
class SendTestNotification:
    deps: NotificationDeps

    async def execute(self, user_id: uuid.UUID) -> list[DeliveryResult]:
        return await NotifyUser(self.deps).execute(
            user_id, Template.TEST, {}, state=self.deps.clock.now().isoformat()
        )


@dataclass(slots=True)
class RecentNotifications:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID, limit: int = 30) -> list[Notification]:
        async with self.uow_factory(user_id) as uow:
            return await uow.notifications.recent(user_id, limit)


@dataclass(slots=True)
class RecordDeliveryReceipt:
    """Provider webhooks (already signature-verified) update delivery status. System scope."""

    system_uow_factory: IUnitOfWorkFactory

    async def execute(
        self, provider_message_id: str, status: NotifStatus, error: str | None = None
    ) -> int:
        async with self.system_uow_factory() as uow:
            count = await uow.notifications.update_by_provider_id(
                provider_message_id, status, error
            )
            await uow.commit()
        return count
