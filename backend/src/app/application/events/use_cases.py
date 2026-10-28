from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.events.models import PipelineEvent
from app.domain.ports.events import IEventBus
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock


@dataclass(slots=True)
class ReplayEvents:
    uow_factory: IUnitOfWorkFactory

    async def execute(
        self, user_id: uuid.UUID, after_seq: int, limit: int = 500
    ) -> list[PipelineEvent]:
        async with self.uow_factory(user_id) as uow:
            return await uow.events.list_since(user_id, after_seq, limit)


@dataclass(slots=True)
class RelayOutbox:
    """Publish committed outbox rows to the event bus. Must be built with the SYSTEM unit of
    work factory (cross-tenant). Rows are claimed with SKIP LOCKED so relays can run in several
    processes; a crash before `mark_published` re-delivers (consumers dedupe by `seq`)."""

    system_uow_factory: IUnitOfWorkFactory
    bus: IEventBus
    clock: Clock
    batch_size: int = 200

    async def execute(self) -> int:
        async with self.system_uow_factory() as uow:
            events = await uow.events.claim_unpublished(self.batch_size)
            if not events:
                return 0
            for event in events:
                await self.bus.publish(event.user_id, event.to_wire())
            await uow.events.mark_published([e.id for e in events], self.clock.now())
            await uow.commit()
        return len(events)
