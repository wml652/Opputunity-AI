"""Latest live-viewport frame per (user, application).

Frames are throwaway JPEG thumbnails of the portal page being filled - never written to disk or
the database, and keyed by user so one tenant can never read another's frame.

* InMemoryLiveFrameStore: single process (dev / in-process execution), evicted LRU.
* RedisLiveFrameStore: multi-process (arq workers fill, the API serves the frame); each frame
  expires after FRAME_TTL_S so a finished run leaves nothing behind."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from redis.asyncio import Redis

MAX_FRAMES = 64
FRAME_TTL_S = 600


class InMemoryLiveFrameStore:
    def __init__(self, max_frames: int = MAX_FRAMES) -> None:
        self._frames: OrderedDict[tuple[uuid.UUID, uuid.UUID], tuple[int, bytes]] = OrderedDict()
        self._seq = 0
        self._max = max_frames
        self._lock = threading.Lock()

    async def put(self, user_id: uuid.UUID, application_id: uuid.UUID, frame: bytes) -> int:
        with self._lock:
            self._seq += 1
            key = (user_id, application_id)
            self._frames[key] = (self._seq, frame)
            self._frames.move_to_end(key)
            while len(self._frames) > self._max:
                self._frames.popitem(last=False)
            return self._seq

    async def get(self, user_id: uuid.UUID, application_id: uuid.UUID) -> tuple[int, bytes] | None:
        with self._lock:
            return self._frames.get((user_id, application_id))


class RedisLiveFrameStore:
    def __init__(self, redis: Redis, *, ttl_s: int = FRAME_TTL_S) -> None:
        self._redis = redis
        self._ttl = ttl_s

    @staticmethod
    def _key(user_id: uuid.UUID, application_id: uuid.UUID) -> str:
        return f"live-frame:{user_id}:{application_id}"

    async def put(self, user_id: uuid.UUID, application_id: uuid.UUID, frame: bytes) -> int:
        seq = int(await self._redis.incr("live-frame:seq"))
        await self._redis.set(
            self._key(user_id, application_id), str(seq).encode() + b"\n" + frame, ex=self._ttl
        )
        return seq

    async def get(self, user_id: uuid.UUID, application_id: uuid.UUID) -> tuple[int, bytes] | None:
        raw = await self._redis.get(self._key(user_id, application_id))
        if not raw:
            return None
        head, _, frame = bytes(raw).partition(b"\n")
        return int(head), frame
