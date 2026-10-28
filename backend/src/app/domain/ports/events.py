from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol


class IEventSubscription(Protocol):
    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...

    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Next message, or None if `timeout` seconds pass without one."""
        ...


class IEventBus(Protocol):
    async def publish(self, user_id: uuid.UUID, message: dict[str, Any]) -> None: ...

    def subscribe(self, user_id: uuid.UUID) -> AbstractAsyncContextManager[IEventSubscription]: ...


class IWorkerRegistry(Protocol):
    async def heartbeat(self, worker_id: str, info: dict[str, Any], ttl_seconds: int) -> None: ...

    async def active_workers(self) -> list[dict[str, Any]]: ...
