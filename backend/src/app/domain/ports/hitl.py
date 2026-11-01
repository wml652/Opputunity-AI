"""Remote human-in-the-loop browser sessions (cloud servers have no screen).

A headless Chromium on the server streams its viewport into the console; the user's clicks and
keystrokes travel back. Used by Teach Mode, portal sign-in ("Sign In & Resume") and bot
challenges. Nothing here acts on the user's behalf: every input event comes from the person
looking at the stream. Input events are relayed, never stored or logged (they may contain a
password the user is typing into the portal)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

TEACH = "TEACH"  # other kinds are SessionKind values (PORTAL, CHALLENGE, LINKEDIN)
VIEWPORT = (1280, 900)


@dataclass(frozen=True, slots=True)
class HitlSession:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    title: str
    url: str
    application_id: uuid.UUID | None = None
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "kind": self.kind,
            "title": self.title,
            "url": self.url,
            "application_id": str(self.application_id) if self.application_id else None,
            "started_at": self.started_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> HitlSession:
        app = d.get("application_id")
        return HitlSession(
            id=uuid.UUID(str(d["id"])),
            user_id=uuid.UUID(str(d["user_id"])),
            kind=str(d["kind"]),
            title=str(d.get("title") or ""),
            url=str(d.get("url") or ""),
            application_id=uuid.UUID(str(app)) if app else None,
            started_at=str(d.get("started_at") or ""),
        )


class IFrameSubscription(Protocol):
    async def get(self, timeout: float) -> bytes | None:
        """Next JPEG frame, or None if `timeout` seconds pass without one."""
        ...


class IRemoteBrowserHub(Protocol):
    async def open(self, session: HitlSession) -> None: ...

    async def close(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None: ...

    async def get(self, user_id: uuid.UUID, session_id: uuid.UUID) -> HitlSession | None:
        """The session if it is open AND belongs to `user_id` (never another tenant's)."""
        ...

    async def list(self, user_id: uuid.UUID) -> Sequence[HitlSession]: ...

    async def send_input(
        self, user_id: uuid.UUID, session_id: uuid.UUID, event: dict[str, Any]
    ) -> bool:
        """Relay one input event to the browser; False if the session is not open."""
        ...

    async def latest_frame(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bytes | None: ...

    def frames(
        self, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> AbstractAsyncContextManager[IFrameSubscription]: ...
