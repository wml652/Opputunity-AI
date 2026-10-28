from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.ports.events import IWorkerRegistry


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    active: int
    workers: list[dict[str, Any]]


@dataclass(slots=True)
class GetWorkerStatus:
    registry: IWorkerRegistry

    async def execute(self) -> WorkerStatus:
        workers = await self.registry.active_workers()
        return WorkerStatus(active=len(workers), workers=workers)
