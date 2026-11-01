"""Redis relay for remote human-in-the-loop browser sessions.

* registry  `hitl:sessions:{user}` (hash: session id -> JSON), refreshed while the session runs,
  so a WebSocket on ANY API replica can check ownership;
* frames    pub/sub `hitl:{session}:frames` (raw JPEG) + `hitl:{session}:frame` (latest, short
  TTL) so a viewer that connects late sees the page at once;
* input     pub/sub `hitl:{session}:input` (JSON events from the viewer to the browser).

Input events are never persisted or logged: Redis pub/sub keeps nothing, and this module never
writes an event's content anywhere (the user may be typing a portal password)."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.domain.ports.hitl import HitlSession

SESSION_TTL_S = 3_600  # a session's registry entry outlives no browser by more than this
FRAME_TTL_S = 120


def _registry(user_id: uuid.UUID) -> str:
    return f"hitl:sessions:{user_id}"


def _frames(session_id: uuid.UUID) -> str:
    return f"hitl:{session_id}:frames"


def _latest(session_id: uuid.UUID) -> str:
    return f"hitl:{session_id}:frame"


def _input(session_id: uuid.UUID) -> str:
    return f"hitl:{session_id}:input"


async def _next_message(pubsub: PubSub, timeout: float) -> dict[str, Any] | None:
    """The next published message within `timeout` (subscribe confirmations are skipped; a
    bare get_message returns None for them even when a real message is right behind)."""
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
        if message is not None and message.get("type") == "message":
            return dict(message)
    return None


class _FrameSubscription:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    async def get(self, timeout: float) -> bytes | None:
        message = await _next_message(self._pubsub, timeout)
        if message is None:
            return None
        data = message["data"]
        return bytes(data) if isinstance(data, bytes | bytearray) else None


class _InputSubscription:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    async def get(self, timeout: float) -> dict[str, Any] | None:
        message = await _next_message(self._pubsub, timeout)
        if message is None:
            return None
        try:
            decoded = json.loads(message["data"])
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None


class RedisRemoteBrowserHub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # -- registry (IRemoteBrowserHub) -------------------------------------------------------------

    async def open(self, session: HitlSession) -> None:
        key = _registry(session.user_id)
        await self._redis.hset(key, str(session.id), json.dumps(session.to_dict()))  # type: ignore[misc]  # redis-py sync|async union
        await self._redis.expire(key, SESSION_TTL_S)

    async def touch(self, session: HitlSession) -> None:
        await self._redis.expire(_registry(session.user_id), SESSION_TTL_S)

    async def close(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        await self._redis.hdel(_registry(user_id), str(session_id))  # type: ignore[misc]  # redis-py sync|async union
        await self._redis.delete(_latest(session_id))
        # Viewers waiting on frames see an empty frame message = "the session ended".
        await self._redis.publish(_frames(session_id), b"")

    async def get(self, user_id: uuid.UUID, session_id: uuid.UUID) -> HitlSession | None:
        raw = await self._redis.hget(_registry(user_id), str(session_id))  # type: ignore[misc]  # redis-py sync|async union
        if not raw:
            return None
        session = HitlSession.from_dict(json.loads(raw))
        return session if session.user_id == user_id else None

    async def list(self, user_id: uuid.UUID) -> Sequence[HitlSession]:
        raw = await self._redis.hgetall(_registry(user_id))  # type: ignore[misc]  # redis-py sync|async union
        sessions = [HitlSession.from_dict(json.loads(v)) for v in raw.values()]
        return sorted((s for s in sessions if s.user_id == user_id), key=lambda s: s.started_at)

    async def send_input(
        self, user_id: uuid.UUID, session_id: uuid.UUID, event: dict[str, Any]
    ) -> bool:
        if await self.get(user_id, session_id) is None:
            return False
        await self._redis.publish(_input(session_id), json.dumps(event))
        return True

    async def latest_frame(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bytes | None:
        if await self.get(user_id, session_id) is None:
            return None
        raw = await self._redis.get(_latest(session_id))
        return bytes(raw) if raw else None

    @contextlib.asynccontextmanager
    async def frames(
        self, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> AsyncIterator[_FrameSubscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_frames(session_id))
        try:
            yield _FrameSubscription(pubsub)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(_frames(session_id))
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    # -- browser side (used by RemoteView) --------------------------------------------------------

    async def publish_frame(self, session_id: uuid.UUID, jpeg: bytes) -> None:
        await self._redis.set(_latest(session_id), jpeg, ex=FRAME_TTL_S)
        await self._redis.publish(_frames(session_id), jpeg)

    @contextlib.asynccontextmanager
    async def inputs(self, session_id: uuid.UUID) -> AsyncIterator[_InputSubscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_input(session_id))
        try:
            yield _InputSubscription(pubsub)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(_input(session_id))
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]
