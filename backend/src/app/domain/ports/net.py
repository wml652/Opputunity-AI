from __future__ import annotations

from typing import Protocol


class IUrlGuard(Protocol):
    async def validate(self, url: str) -> str:
        """Return the normalized URL or raise `UnsafeUrlError`."""
        ...
