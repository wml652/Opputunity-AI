"""Notification vocabulary (pure): channels, templates, deduplication."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.shared.enums import NotifChannel, NotifStatus


class Template(enum.StrEnum):
    QUESTION_NEEDED = "question_needed"
    READY_FOR_OK = "ready_for_ok"
    SUBMITTED = "submitted"
    FAILED = "failed"
    CHANNEL_VERIFY = "channel_verify"
    TEST = "test"


@dataclass(slots=True)
class NotificationChannel:
    id: uuid.UUID
    user_id: uuid.UUID
    channel: NotifChannel
    address: str  # plaintext in the domain; encrypted at rest
    address_hint: str
    enabled: bool
    verified_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class Notification:
    id: uuid.UUID
    user_id: uuid.UUID
    channel: NotifChannel
    template: Template
    dedupe_key: str
    payload: dict[str, Any]
    status: NotifStatus
    provider: str | None
    provider_message_id: str | None
    error: str | None
    related_application_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str
    text: str


def mask_address(channel: NotifChannel, address: str) -> str:
    if channel is NotifChannel.EMAIL and "@" in address:
        name, _, domain = address.partition("@")
        return f"{name[:2]}***@{domain}"
    digits = "".join(c for c in address if c.isdigit())
    return f"+***{digits[-4:]}" if len(digits) >= 4 else "***"


def dedupe_key(template: Template, application_id: uuid.UUID | None, state: str | None) -> str:
    return f"{template.value}:{application_id or '-'}:{state or '-'}"


def render(template: Template, payload: dict[str, Any], console_url: str) -> RenderedMessage:
    """No PII and no one-tap actions: messages only point back to the authenticated console."""
    title = str(payload.get("title") or "an application")
    link = f"{console_url.rstrip('/')}{payload.get('path', '/inbox')}"
    if template is Template.QUESTION_NEEDED:
        n = int(payload.get("questions", 1))
        return RenderedMessage(
            f"Input needed: {title}",
            f"OPPORTUNITIES ON THE GO paused '{title}' because it needs {n} answer(s) from you. "
            f"Answer in the Action Inbox: {link}",
        )
    if template is Template.READY_FOR_OK:
        return RenderedMessage(
            f"Ready for your OK: {title}",
            f"'{title}' is filled up to the final step and waiting for your review. Nothing has "
            f"been submitted. Review and approve: {link}",
        )
    if template is Template.SUBMITTED:
        return RenderedMessage(f"Submitted: {title}", f"'{title}' was submitted. Receipt: {link}")
    if template is Template.FAILED:
        return RenderedMessage(
            f"Needs attention: {title}", f"'{title}' could not continue. Details: {link}"
        )
    if template is Template.CHANNEL_VERIFY:
        return RenderedMessage(
            "Verify your notification channel",
            f"Your OPPORTUNITIES ON THE GO verification code is {payload.get('code')}. "
            "It expires in 15 minutes.",
        )
    return RenderedMessage(
        "Test notification", "This is a test notification from OPPORTUNITIES ON THE GO."
    )
