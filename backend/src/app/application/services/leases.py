"""Run leases for durable execution: a worker that owns a campaign or a fill renews a short lease
while it runs. A lease that expires means the worker died, and the resume sweep restarts the run
from its checkpoint (bounded by MAX_RESUMES)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

LEASE_TTL = timedelta(seconds=120)
RENEW_EVERY_S = 30.0
MAX_RESUMES = 3
# A resumed run waits in the queue under this owner; any worker may claim it. If no worker
# claims it within LEASE_TTL (the job was lost), the sweep resumes it again.
PENDING_OWNER = "resume-pending"


def run_owner(runner_id: str) -> str:
    """One owner per run (two runs in one process never share a lease)."""
    return f"{runner_id}/{uuid.uuid4().hex[:8]}"[:120]


@contextlib.asynccontextmanager
async def kept_lease(
    renew: Callable[[], Awaitable[object]], every_s: float = RENEW_EVERY_S
) -> AsyncIterator[None]:
    """Renews the lease in the background while the body runs."""

    async def loop() -> None:
        while True:
            await asyncio.sleep(every_s)
            with contextlib.suppress(Exception):  # a DB hiccup: the next renewal retries
                await renew()

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
