from __future__ import annotations

import asyncio
import uuid

from app.application.applications.use_cases import RunApplication
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class InProcessApplicationRunner:
    """Runs one filling run at a time per application inside the API process (dev mode)."""

    def __init__(self, run: RunApplication) -> None:
        self._run = run
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def start(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        current = self._tasks.get(application_id)
        if current is not None and not current.done():
            return  # already filling; the run will pick up the latest state
        task = asyncio.create_task(
            self._guarded(user_id, application_id), name=f"application-{application_id}"
        )
        self._tasks[application_id] = task

    async def _guarded(self, user_id: uuid.UUID, application_id: uuid.UUID) -> None:
        try:
            await self._run.execute(user_id, application_id)
        except Exception:
            log.exception("application.runner_crashed", application_id=str(application_id))

    async def wait_idle(self, timeout: float = 120.0) -> None:
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
