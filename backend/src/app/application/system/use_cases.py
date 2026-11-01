from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.ports.events import IWorkerRegistry
from app.domain.ports.system import IReadinessProbe, ISystemResources


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


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    checks: dict[str, bool]
    free_memory_mb: int | None


@dataclass(slots=True)
class CheckReadiness:
    """Dependencies the API cannot serve without (database, Redis) plus free memory."""

    probes: Sequence[IReadinessProbe]
    resources: ISystemResources

    async def execute(self) -> Readiness:
        results = await asyncio.gather(*(p.check() for p in self.probes))
        checks = {p.name: ok for p, ok in zip(self.probes, results, strict=True)}
        return Readiness(all(results), checks, self.resources.free_memory_mb())
