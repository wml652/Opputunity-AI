"""Global browser pool: at most N Chromium instances at once, across every worker process.

A Redis sorted set per pool holds one member per running browser, scored by its lease expiry.
Acquiring removes expired members (a crashed process frees its slots within `ttl_s`), then adds
a member only if the pool is below its limit - inside an optimistic WATCH/MULTI transaction so
two processes can never both take the last slot. The holder renews its score while it runs.

Pools: "auto" (fills, submissions, crawler browsers) and "hitl" (remote human sessions: Teach
Mode, sign-in / challenge captures) so a person is never stuck behind a batch of fills."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.infrastructure.logging import get_logger

log = get_logger(__name__)

AUTO = "auto"
HITL = "hitl"
KEY_PREFIX = "browser-slots"


class RedisBrowserSlots:
    def __init__(
        self,
        redis: Redis,
        limits: dict[str, int],
        *,
        ttl_s: float = 60.0,
        poll_s: float = 0.5,
        key_prefix: str = KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self._limits = limits
        self._ttl = ttl_s
        self._poll = poll_s
        self._prefix = key_prefix

    def _key(self, kind: str) -> str:
        return f"{self._prefix}:{kind}"

    async def in_use(self, kind: str = AUTO) -> int:
        key = self._key(kind)
        await self._redis.zremrangebyscore(key, "-inf", time.time())
        return int(await self._redis.zcard(key))

    async def _try_acquire(self, key: str, token: str, limit: int) -> bool:
        await self._redis.zremrangebyscore(key, "-inf", time.time())
        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if int(await pipe.zcard(key)) >= limit:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.zadd(key, {token: time.time() + self._ttl})
                await pipe.execute()
            except WatchError:
                return False  # another process changed the pool; poll again
        return True

    @contextlib.asynccontextmanager
    async def slot(self, kind: str = AUTO) -> AsyncIterator[None]:
        limit = max(1, self._limits.get(kind, 1))
        key = self._key(kind)
        token = uuid.uuid4().hex
        waited = 0.0
        while not await self._try_acquire(key, token, limit):
            if waited == 0.0:
                log.info("browser_slots.waiting", pool=kind, limit=limit)
            await asyncio.sleep(self._poll)
            waited += self._poll

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._ttl / 3)
                with contextlib.suppress(Exception):
                    await self._redis.zadd(key, {token: time.time() + self._ttl})

        keeper = asyncio.create_task(renew())
        try:
            yield
        finally:
            keeper.cancel()
            with contextlib.suppress(BaseException):
                await keeper
            with contextlib.suppress(Exception):
                await asyncio.shield(self._redis.zrem(key, token))
