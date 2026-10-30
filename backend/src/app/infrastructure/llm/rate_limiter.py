"""Proactive request pacing for LLM providers (sliding-window token bucket).

Hosted gateways enforce a requests-per-minute quota (build.nvidia.com: 40 RPM per key). Reacting
to HTTP 429 wastes a round trip plus a `Retry-After` stall per call; with several extraction and
eligibility workers bursting together that adds up to minutes. Instead every call to the same
(host, API key) first takes a slot from a shared limiter: at most `rpm` request starts in any
rolling `period`. Callers queue FIFO, so concurrent workers run at the maximum rate the quota
allows and never trip it."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Awaitable, Callable

DEFAULT_RPM = 36  # a safety margin under build.nvidia.com's 40 RPM


class SlidingWindowRateLimiter:
    def __init__(
        self,
        rpm: int,
        *,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rpm = max(1, rpm)
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.total_waited = 0.0
        self.acquired = 0

    async def acquire(self) -> float:
        """Wait (if needed) for a free slot; returns the seconds waited."""
        waited = 0.0
        async with self._lock:  # FIFO: waiters are served in arrival order
            while True:
                now = self._clock()
                while self._starts and self._starts[0] <= now - self.period:
                    self._starts.popleft()
                if len(self._starts) < self.rpm:
                    self._starts.append(now)
                    self.acquired += 1
                    self.total_waited += waited
                    return waited
                delay = self._starts[0] + self.period - now
                await self._sleep(max(delay, 0.001))
                waited += max(delay, 0.001)


class RateLimiterRegistry:
    """One limiter per (host, API key): every adapter sharing a key shares its quota."""

    def __init__(self) -> None:
        self._limiters: dict[str, SlidingWindowRateLimiter] = {}

    def get(self, base_url: str, api_key: str | None, rpm: int | None) -> SlidingWindowRateLimiter:
        host = base_url.split("://", 1)[-1].split("/", 1)[0].lower()
        digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:16]
        key = f"{host}|{digest}"
        limiter = self._limiters.get(key)
        wanted = rpm or DEFAULT_RPM
        if limiter is None:
            limiter = self._limiters[key] = SlidingWindowRateLimiter(wanted)
        elif limiter.rpm != wanted:
            limiter.rpm = max(1, wanted)  # the user edited the limit: apply it live
        return limiter
