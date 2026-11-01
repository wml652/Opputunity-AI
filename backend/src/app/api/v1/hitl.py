"""Remote human-in-the-loop browser sessions (Teach Mode, portal sign-in, bot challenges).

`GET /hitl/sessions` lists the caller's open sessions; the console opens
`WS /hitl/sessions/{id}/stream`: the server sends a `meta` JSON message, then JPEG frames as
binary messages (and `{"type": "closed"}` when the session ends); the client sends input events
as JSON text. The WebSocket is cookie-authenticated, checks the Origin against the console's
origins (cross-site WebSocket hijacking), and only ever serves the caller's own session.

Input events are validated and relayed - never logged or stored (a user may be typing a portal
password)."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.api.container import ApiContainer
from app.api.cookies import ACCESS_COOKIE
from app.api.deps import Container, CurrentUserId
from app.domain.ports.hitl import VIEWPORT, HitlSession, IRemoteBrowserHub
from app.domain.shared.errors import DomainError, NotFoundError

router = APIRouter(tags=["hitl"])

MAX_MESSAGE_CHARS = 8_192
MAX_EVENTS_PER_S = 120  # mouse moves are throttled client-side; this only stops floods
FRAME_WAIT_S = 10.0
# WebSocket close codes (application range 4000-4999)
WS_UNAUTHORIZED, WS_FORBIDDEN_ORIGIN, WS_NOT_FOUND = 4401, 4403, 4404


class HitlSessionOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    url: str
    application_id: uuid.UUID | None
    started_at: str

    @staticmethod
    def of(s: HitlSession) -> HitlSessionOut:
        return HitlSessionOut(
            id=s.id,
            kind=s.kind,
            title=s.title,
            url=s.url,
            application_id=s.application_id,
            started_at=s.started_at,
        )


@router.get("/hitl/sessions", response_model=list[HitlSessionOut])
async def list_sessions(user_id: CurrentUserId, container: Container) -> list[HitlSessionOut]:
    if container.hitl is None:
        return []
    return [HitlSessionOut.of(s) for s in await container.hitl.list(user_id)]


@router.post("/hitl/sessions/{session_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    """'Done' in the console: closes the remote browser (like closing a local window)."""
    if container.hitl is None or not await container.hitl.send_input(
        user_id, session_id, {"type": "close"}
    ):
        raise NotFoundError("no open browser session")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- input events -----------------------------------------------------------------------------


class MouseIn(BaseModel):
    type: Literal["mouse"]
    action: Literal["move", "down", "up", "click", "wheel"]
    x: float = Field(ge=0, le=VIEWPORT[0])
    y: float = Field(ge=0, le=VIEWPORT[1])
    button: Literal["left", "right", "middle"] = "left"
    dx: float = Field(0, ge=-4_000, le=4_000)
    dy: float = Field(0, ge=-4_000, le=4_000)


class KeyIn(BaseModel):
    type: Literal["key"]
    action: Literal["down", "up"]
    key: str = Field(min_length=1, max_length=32)


class TextIn(BaseModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=4_000)


class NavIn(BaseModel):
    type: Literal["nav"]
    action: Literal["back", "forward", "reload"]


class ControlIn(BaseModel):
    type: Literal["finish", "close"]


InputEvent = Annotated[MouseIn | KeyIn | TextIn | NavIn | ControlIn, Field(discriminator="type")]
_EVENT = TypeAdapter(InputEvent)


def parse_input(raw: str) -> dict[str, Any] | None:
    """A validated input event, or None (oversized / malformed messages are dropped)."""
    if len(raw) > MAX_MESSAGE_CHARS:
        return None
    try:
        event = _EVENT.validate_json(raw)
    except ValidationError:
        return None
    return event.model_dump()


# -- stream -----------------------------------------------------------------------------------


async def _ws_user(websocket: WebSocket, container: ApiContainer) -> uuid.UUID | None:
    if container.config.dev_auth_bypass:
        cached: uuid.UUID | None = getattr(websocket.app.state, "dev_user_id", None)
        if cached is None:
            cached = await container.ensure_dev_user.execute()
            websocket.app.state.dev_user_id = cached
        return cached
    token = websocket.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        return container.access_tokens.verify(token).user_id
    except DomainError:
        return None


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """Browsers always send Origin on WebSockets; a cross-site page must not ride the cookies."""
    if origin is None:
        return True  # not a browser: it cannot carry the user's cookies cross-site
    return origin.rstrip("/") in {o.rstrip("/") for o in allowed}


@router.websocket("/hitl/sessions/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: uuid.UUID) -> None:
    container: ApiContainer = websocket.app.state.container
    hub = container.hitl
    if not origin_allowed(websocket.headers.get("origin"), container.config.cors_origins):
        await websocket.close(code=WS_FORBIDDEN_ORIGIN)
        return
    user_id = await _ws_user(websocket, container)
    if user_id is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return
    session = await hub.get(user_id, session_id) if hub is not None else None
    if hub is None or session is None:
        await websocket.close(code=WS_NOT_FOUND)
        return
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "meta",
            "viewport": {"width": VIEWPORT[0], "height": VIEWPORT[1]},
            "kind": session.kind,
            "title": session.title,
        }
    )
    latest = await hub.latest_frame(user_id, session_id)
    if latest:
        await websocket.send_bytes(latest)
    async with hub.frames(user_id, session_id) as frames:
        pump = asyncio.create_task(_pump_frames(websocket, hub, frames, user_id, session_id))
        relay = asyncio.create_task(_relay_input(websocket, hub, user_id, session_id))
        done, pending = await asyncio.wait({pump, relay}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending | done:
            with contextlib.suppress(BaseException):
                await task
    with contextlib.suppress(Exception):
        await websocket.close()


async def _pump_frames(
    websocket: WebSocket,
    hub: IRemoteBrowserHub,
    frames: Any,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    while True:
        frame = await frames.get(timeout=FRAME_WAIT_S)
        if frame is None:  # quiet page: is the session still open?
            if await hub.get(user_id, session_id) is None:
                await websocket.send_json({"type": "closed"})
                return
            # Keepalive: cloud ingresses (Azure Container Apps: 240 s) drop idle WebSockets.
            await websocket.send_json({"type": "ping"})
            continue
        if frame == b"":  # the hub's "session ended" marker
            await websocket.send_json({"type": "closed"})
            return
        await websocket.send_bytes(frame)


async def _relay_input(
    websocket: WebSocket, hub: IRemoteBrowserHub, user_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    window_start, count = time.monotonic(), 0
    try:
        while True:
            raw = await websocket.receive_text()
            now = time.monotonic()
            if now - window_start >= 1.0:
                window_start, count = now, 0
            count += 1
            if count > MAX_EVENTS_PER_S:
                continue
            event = parse_input(raw)
            if event is not None and not await hub.send_input(user_id, session_id, event):
                return  # the session ended
    except WebSocketDisconnect:
        return
