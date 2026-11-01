"""Live application viewport: the filler's steps become `application.live_step` SSE events and its
JPEG frames go to the latest-frame store (served at /applications/{id}/live-frame).

Events carry no field values - only the label being filled and the value's source - and are
throttled so a long form does not flood the outbox."""

from __future__ import annotations

import time
import uuid

from app.domain.applications.filling import LiveStep
from app.domain.events.models import NewEvent
from app.domain.ports.applications import FillObserver, ILiveFrameStore
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.errors import NotFoundError

EVENT_INTERVAL_S = 0.5


def live_observer(
    uow_factory: IUnitOfWorkFactory,
    frames: ILiveFrameStore | None,
    clock: Clock,
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    title: str,
    state: str,
) -> FillObserver | None:
    if frames is None:
        return None
    last = [0.0]

    async def observe(step: LiveStep, frame: bytes | None) -> None:
        seq = 0
        if frame:
            seq = await frames.put(user_id, application_id, frame)
        else:
            stored = await frames.get(user_id, application_id)
            seq = stored[0] if stored else 0
        now = time.monotonic()
        if not (step.final or frame or now - last[0] >= EVENT_INTERVAL_S):
            return
        last[0] = now
        async with uow_factory(user_id) as uow:
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="application.live_step",
                    application_id=application_id,
                    payload={
                        "application_id": str(application_id),
                        "opportunity_title": title,
                        "state": state,
                        "phase_label": step.phase_label,
                        "current_url": step.current_url,
                        "step_index": step.step_index,
                        "field_label": step.field_label,
                        "value_source": step.value_source,
                        "progress_pct": step.progress_pct,
                        "frame_seq": seq,
                        "final": step.final,
                        "updated_at": clock.now().isoformat(),
                    },
                )
            )
            await uow.commit()

    return observe


class GetLiveFrame:
    def __init__(self, frames: ILiveFrameStore | None) -> None:
        self._frames = frames

    async def execute(self, user_id: uuid.UUID, application_id: uuid.UUID) -> tuple[int, bytes]:
        found = await self._frames.get(user_id, application_id) if self._frames else None
        if found is None:
            raise NotFoundError("no live frame for this application yet")
        return found
