"""Process health: readiness probes and host resources (memory admission for browsers)."""

from __future__ import annotations

from typing import Protocol


class IReadinessProbe(Protocol):
    name: str

    async def check(self) -> bool:
        """True when the dependency answers (never raises)."""
        ...


class ISystemResources(Protocol):
    def free_memory_mb(self) -> int | None:
        """Available physical memory, or None when it cannot be measured."""
        ...
