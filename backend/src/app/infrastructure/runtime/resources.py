"""Readiness probes (database, Redis) and host memory measurement."""

from __future__ import annotations

import asyncio

import psutil  # type: ignore[import-untyped]  # no bundled stubs
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.ports.system import ISystemResources
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

PROBE_TIMEOUT_S = 3.0


class PsutilResources:
    def free_memory_mb(self) -> int | None:
        try:
            return int(psutil.virtual_memory().available // (1024 * 1024))
        except Exception:  # noqa: BLE001 - unmeasurable is "unknown", never a crash
            return None


class DatabaseProbe:
    name = "database"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> bool:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S), self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - reported as not ready
            log.warning("ready.database_down", error=type(exc).__name__)
            return False
        return True


class RedisProbe:
    name = "redis"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self) -> bool:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                await self._redis.ping()
        except Exception as exc:  # noqa: BLE001 - reported as not ready
            log.warning("ready.redis_down", error=type(exc).__name__)
            return False
        return True


async def wait_for_memory(
    resources: ISystemResources, min_free_mb: int, *, max_wait_s: float = 120.0
) -> bool:
    """Memory admission before launching a browser: wait (polling) while free memory is below
    `min_free_mb`. Returns False if it is still low after `max_wait_s` (the caller proceeds but
    logs it: a stranded application is worse than one slow start)."""
    waited = 0.0
    while True:
        free = resources.free_memory_mb()
        if free is None or free >= min_free_mb:
            return True
        if waited >= max_wait_s:
            return False
        if waited == 0.0:
            log.warning("memory.low_waiting", free_mb=free, min_free_mb=min_free_mb)
        await asyncio.sleep(5.0)
        waited += 5.0
