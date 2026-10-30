from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from app.application.applications.submission import RunSubmission
from app.application.applications.use_cases import RunApplication
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class InProcessApplicationRunner:
    """Runs one filling run at a time per application inside the API process (dev mode)."""

    def __init__(
        self,
        run: RunApplication,
        submit: RunSubmission | None = None,
        *,
        max_parallel: int = 2,
    ) -> None:
        self._run = run
        self._submit = submit
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        # Batch auto-stage may start many runs: at most `max_parallel` browsers at once.
        self._slots = asyncio.Semaphore(max(1, max_parallel))

    async def start(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        self._spawn(user_id, application_id, self._run.execute, "fill")

    async def submit(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        if self._submit is None:
            raise RuntimeError("submission runner not configured")
        self._spawn(user_id, application_id, self._submit.execute, "submit")

    def _spawn(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        job: Callable[[uuid.UUID, uuid.UUID], Awaitable[object]],
        kind: str,
    ) -> None:
        current = self._tasks.get(application_id)
        if current is not None and not current.done():
            return  # one run per application; the next run picks up the latest state
        task = asyncio.create_task(
            self._guarded(user_id, application_id, job, kind),
            name=f"application-{kind}-{application_id}",
        )
        self._tasks[application_id] = task

    async def _guarded(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        job: Callable[[uuid.UUID, uuid.UUID], Awaitable[object]],
        kind: str,
    ) -> None:
        try:
            async with self._slots:
                await job(user_id, application_id)
        except Exception:
            log.exception(
                "application.runner_crashed", application_id=str(application_id), kind=kind
            )

    async def wait_idle(self, timeout: float = 120.0) -> None:
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
