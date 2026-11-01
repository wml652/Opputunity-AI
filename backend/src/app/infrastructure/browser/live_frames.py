"""Latest live-viewport frame per (user, application), in process memory.

Frames are throwaway JPEG thumbnails of the portal page being filled - never written to disk or
the database, evicted LRU, and keyed by user so one tenant can never read another's frame. A
multi-process deployment (separate worker) would back this port with Redis instead."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

MAX_FRAMES = 64


class InMemoryLiveFrameStore:
    def __init__(self, max_frames: int = MAX_FRAMES) -> None:
        self._frames: OrderedDict[tuple[uuid.UUID, uuid.UUID], tuple[int, bytes]] = OrderedDict()
        self._seq = 0
        self._max = max_frames
        self._lock = threading.Lock()

    def put(self, user_id: uuid.UUID, application_id: uuid.UUID, frame: bytes) -> int:
        with self._lock:
            self._seq += 1
            key = (user_id, application_id)
            self._frames[key] = (self._seq, frame)
            self._frames.move_to_end(key)
            while len(self._frames) > self._max:
                self._frames.popitem(last=False)
            return self._seq

    def get(self, user_id: uuid.UUID, application_id: uuid.UUID) -> tuple[int, bytes] | None:
        with self._lock:
            return self._frames.get((user_id, application_id))
