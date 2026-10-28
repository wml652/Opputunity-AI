"""Outbox relay loop: wakes on Postgres NOTIFY (trigger on `pipeline_events` INSERT) with a
polling fallback, then drains unpublished rows to Redis via the `RelayOutbox` use case."""

from __future__ import annotations

import asyncio
import contextlib

import asyncpg

from app.application.events.use_cases import RelayOutbox
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

NOTIFY_CHANNEL = "pipeline_events"


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class OutboxRelayLoop:
    def __init__(
        self,
        relay: RelayOutbox,
        *,
        database_url: str,
        poll_interval: float,
        use_listen: bool = True,
    ) -> None:
        self._relay = relay
        self._dsn = asyncpg_dsn(database_url)
        self._poll_interval = poll_interval
        self._use_listen = use_listen
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._conn: asyncpg.Connection | None = None

    def _on_notify(self, *_args: object) -> None:
        self._wake.set()

    async def _listen(self) -> None:
        try:
            self._conn = await asyncpg.connect(self._dsn)
            await self._conn.add_listener(NOTIFY_CHANNEL, self._on_notify)
        except Exception as exc:  # noqa: BLE001 - fall back to polling
            log.warning("outbox.listen_unavailable", error=str(exc))
            self._conn = None

    async def drain(self) -> int:
        total = 0
        while True:
            published = await self._relay.execute()
            total += published
            if published == 0:
                return total

    async def run_forever(self) -> None:
        if self._use_listen:
            await self._listen()
        while True:
            try:
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep relaying after transient failures
                log.error("outbox.relay_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            self._wake.clear()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="outbox-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
