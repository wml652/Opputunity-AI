"""Notification adapters behind `INotifier`, plus webhook signature verification.

Providers are configured from environment settings; none is required. Without a configured
provider for a channel, `DevLogNotifier` writes the message to `<logs_dir>/notifications.log`
(clearly labeled as not delivered) so the whole flow can be exercised locally."""

from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

from app.application.notifications.use_cases import NotificationError
from app.domain.notification.models import RenderedMessage
from app.domain.shared.enums import NotifChannel
from app.infrastructure.logging import redact_text


class SmtpNotifier:
    name = "smtp"
    channel = NotifChannel.EMAIL

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        sender: str,
        starttls: bool = True,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._starttls = sender, starttls

    def _send_sync(self, address: str, message: RenderedMessage) -> str:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, address, message.subject
        msg_id = f"<{uuid.uuid4().hex}@opportunities-on-the-go>"
        msg["Message-ID"] = msg_id
        msg.set_content(message.text)
        with smtplib.SMTP(self._host, self._port, timeout=20) as smtp:
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)
        return msg_id

    async def send(self, address: str, message: RenderedMessage) -> str:
        try:
            return await asyncio.to_thread(self._send_sync, address, message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(redact_text(f"smtp: {exc}")[:300]) from exc


class ResendNotifier:
    name = "resend"
    channel = NotifChannel.EMAIL

    def __init__(self, api_key: str, sender: str, http: httpx.AsyncClient) -> None:
        self._key, self._sender, self._http = api_key, sender, http

    async def send(self, address: str, message: RenderedMessage) -> str:
        r = await self._http.post(
            "https://api.resend.com/emails",
            headers={"authorization": f"Bearer {self._key}"},
            json={
                "from": self._sender,
                "to": [address],
                "subject": message.subject,
                "text": message.text,
            },
            timeout=20,
        )
        if r.status_code >= 400:
            raise NotificationError(redact_text(f"resend HTTP {r.status_code}: {r.text[:200]}"))
        return str(r.json().get("id", ""))


class TwilioWhatsAppNotifier:
    name = "twilio"
    channel = NotifChannel.WHATSAPP

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        http: httpx.AsyncClient,
        status_callback: str | None = None,
    ) -> None:
        self._sid, self._token, self._from = account_sid, auth_token, from_number
        self._http, self._callback = http, status_callback

    async def send(self, address: str, message: RenderedMessage) -> str:
        data = {"From": f"whatsapp:{self._from}", "To": f"whatsapp:{address}", "Body": message.text}
        if self._callback:
            data["StatusCallback"] = self._callback
        r = await self._http.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json",
            data=data,
            auth=(self._sid, self._token),
            timeout=20,
        )
        if r.status_code >= 400:
            raise NotificationError(redact_text(f"twilio HTTP {r.status_code}: {r.text[:200]}"))
        return str(r.json().get("sid", ""))


class MetaWhatsAppNotifier:
    """WhatsApp Cloud API. Business-initiated messages outside the 24h window must use an
    approved template; `template_name` takes the rendered text as its single body parameter."""

    name = "meta"
    channel = NotifChannel.WHATSAPP

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        template_name: str,
        http: httpx.AsyncClient,
        language: str = "en",
    ) -> None:
        self._token, self._phone_id, self._template = access_token, phone_number_id, template_name
        self._http, self._lang = http, language

    async def send(self, address: str, message: RenderedMessage) -> str:
        r = await self._http.post(
            f"https://graph.facebook.com/v21.0/{self._phone_id}/messages",
            headers={"authorization": f"Bearer {self._token}"},
            json={
                "messaging_product": "whatsapp",
                "to": address.lstrip("+"),
                "type": "template",
                "template": {
                    "name": self._template,
                    "language": {"code": self._lang},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": message.text[:1000]}],
                        }
                    ],
                },
            },
            timeout=20,
        )
        if r.status_code >= 400:
            raise NotificationError(redact_text(f"meta HTTP {r.status_code}: {r.text[:200]}"))
        messages = r.json().get("messages") or [{}]
        return str(messages[0].get("id", ""))


class DevLogNotifier:
    """Local development only: records the message on disk instead of delivering it."""

    def __init__(self, channel: NotifChannel, logs_dir: str | Path) -> None:
        self.channel = channel
        self.name = f"devlog-{channel.value.lower()}"
        self._path = Path(logs_dir) / "notifications.log"

    async def send(self, address: str, message: RenderedMessage) -> str:
        msg_id = f"dev-{uuid.uuid4().hex[:12]}"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "id": msg_id,
            "channel": self.channel.value,
            "to": address[:3] + "***",
            "subject": message.subject,
            "text": message.text,
            "note": "NOT DELIVERED: no provider configured (dev log notifier)",
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return msg_id


from app.domain.notification.signatures import (  # noqa: E402 - re-exported for callers
    verify_meta_signature,
    verify_twilio_signature,
)

__all__ = ["verify_meta_signature", "verify_twilio_signature"]
