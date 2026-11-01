from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.application.applications.submission import RunSubmission
from app.application.applications.use_cases import RunApplication
from app.domain.ports.system import IBrowserSlots, ISystemResources
from app.infrastructure.logging import get_logger
from app.infrastructure.runtime.resources import wait_for_memory

log = get_logger(__name__)


async def run_with_browser(
    job: Callable[[uuid.UUID, uuid.UUID], Awaitable[object]],
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    *,
    slots: IBrowserSlots | None,
    resources: ISystemResources | None,
    min_free_mb: int,
) -> None:
    """Each fill / submission launches one Chromium: take a slot in the global browser pool,
    then make sure there is memory for it (never start one into an out-of-memory kill)."""
    slot = slots.slot("auto") if slots is not None else contextlib.nullcontext()
    async with slot:
        if resources is not None and min_free_mb > 0:
            enough = await wait_for_memory(resources, min_free_mb)
            if not enough:
                log.warning(
                    "memory.low_starting_anyway",
                    application_id=str(application_id),
                    min_free_mb=min_free_mb,
                )
        await job(user_id, application_id)


class ArqApplicationRunner:
    """Enqueues fills / submissions on the arq `browser` queue (production / multi-process).
    No arq job id: a finished job's result would block a later run of the same application;
    the run lease (claimed inside RunApplication) already guarantees one run at a time."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def start(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        await self._pool.enqueue_job(
            "run_application", str(user_id), str(application_id), _queue_name="arq:browser"
        )

    async def submit(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        await self._pool.enqueue_job(
            "run_submission", str(user_id), str(application_id), _queue_name="arq:browser"
        )


class InProcessApplicationRunner:
    """Runs one filling run at a time per application inside the API process (dev mode)."""

    def __init__(
        self,
        run: RunApplication,
        submit: RunSubmission | None = None,
        *,
        max_parallel: int = 2,
        resources: ISystemResources | None = None,
        min_free_memory_mb: int = 0,
        slots: IBrowserSlots | None = None,
    ) -> None:
        self._browser_slots = slots
        self._run = run
        self._resources = resources
        self._min_free_mb = min_free_memory_mb
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
                await run_with_browser(
                    job,
                    user_id,
                    application_id,
                    slots=self._browser_slots,
                    resources=self._resources,
                    min_free_mb=self._min_free_mb,
                )
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
