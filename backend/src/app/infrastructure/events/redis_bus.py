from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

CHANNEL_PREFIX = "events:"
WORKER_KEY_PREFIX = "workers:heartbeat:"


def channel_for(user_id: uuid.UUID) -> str:
    return f"{CHANNEL_PREFIX}{user_id}"


class RedisSubscription:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    async def get(self, timeout: float) -> dict[str, Any] | None:
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if message is None or message.get("type") != "message":
            return None
        data = message["data"]
        if isinstance(data, bytes):
            data = data.decode()
        decoded: dict[str, Any] = json.loads(data)
        return decoded

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self.get(timeout=1.0)
            if item is not None:
                yield item


class RedisEventBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, user_id: uuid.UUID, message: dict[str, Any]) -> None:
        await self._redis.publish(channel_for(user_id), json.dumps(message, default=str))

    @asynccontextmanager
    async def subscribe(self, user_id: uuid.UUID) -> AsyncIterator[RedisSubscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel_for(user_id))
        try:
            yield RedisSubscription(pubsub)
        finally:
            await pubsub.unsubscribe(channel_for(user_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]


class RedisWorkerRegistry:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def heartbeat(self, worker_id: str, info: dict[str, Any], ttl_seconds: int) -> None:
        payload = {**info, "worker_id": worker_id, "seen_at": time.time()}
        await self._redis.set(
            f"{WORKER_KEY_PREFIX}{worker_id}", json.dumps(payload), ex=ttl_seconds
        )

    async def active_workers(self) -> list[dict[str, Any]]:
        workers: list[dict[str, Any]] = []
        async for key in self._redis.scan_iter(match=f"{WORKER_KEY_PREFIX}*", count=100):
            raw = await self._redis.get(key)
            if raw:
                workers.append(json.loads(raw))
        return sorted(workers, key=lambda w: str(w.get("worker_id")))
