"""Server-Sent Events: replay from `Last-Event-ID` (the outbox `seq`), then tail Redis pub/sub.

The subscription opens BEFORE the replay query, so an event committed between the two is seen
on at least one path; duplicates are dropped by `seq`."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import Container, CurrentUserId
from app.api.v1.schemas import EventOut, InboxSummaryOut
from app.application.events.use_cases import ReplayEvents
from app.domain.ports.events import IEventBus

router = APIRouter(tags=["events"])

REPLAY_PAGE = 500
RETRY_MS = 3000
_DEDUPE_WINDOW = 2048


def format_sse(message: dict[str, Any]) -> str:
    # No `event:` field: every message is a default "message" event carrying its `type` in the
    # payload, so clients never miss event types they were not written to know about.
    return (
        f"id: {message['seq']}\ndata: {json.dumps(message, separators=(',', ':'), default=str)}\n\n"
    )


async def event_stream(
    *,
    user_id: uuid.UUID,
    after_seq: int,
    replay: ReplayEvents,
    bus: IEventBus,
    heartbeat_seconds: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    delivered: set[int] = set()
    high_water = after_seq

    def remember(seq: int) -> None:
        nonlocal high_water
        delivered.add(seq)
        high_water = max(high_water, seq)
        if len(delivered) > _DEDUPE_WINDOW:
            delivered.discard(min(delivered))

    async with bus.subscribe(user_id) as subscription:
        yield f"retry: {RETRY_MS}\n: connected\n\n"
        cursor = after_seq
        while True:
            page = await replay.execute(user_id, cursor, REPLAY_PAGE)
            for event in page:
                remember(event.seq)
                yield format_sse(event.to_wire())
            if len(page) < REPLAY_PAGE:
                break
            cursor = page[-1].seq

        next_heartbeat = time.monotonic() + heartbeat_seconds
        while not await is_disconnected():
            timeout = max(0.05, min(1.0, next_heartbeat - time.monotonic()))
            message = await subscription.get(timeout=timeout)
            if message is not None:
                seq = int(message.get("seq", 0))
                if seq > after_seq and seq not in delivered:
                    remember(seq)
                    yield format_sse(message)
            if time.monotonic() >= next_heartbeat:
                yield f": heartbeat {high_water}\n\n"
                next_heartbeat = time.monotonic() + heartbeat_seconds


@router.get("/events/stream")
async def stream_events(
    request: Request,
    user_id: CurrentUserId,
    container: Container,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_seq: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    start = after_seq if after_seq is not None else _parse_seq(last_event_id)
    return StreamingResponse(
        event_stream(
            user_id=user_id,
            after_seq=start,
            replay=container.replay_events,
            bus=container.event_bus,
            heartbeat_seconds=container.config.sse_heartbeat_seconds,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _parse_seq(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 0


@router.get("/events", response_model=list[EventOut])
async def list_events(
    user_id: CurrentUserId,
    container: Container,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=REPLAY_PAGE),
) -> list[EventOut]:
    events = await container.replay_events.execute(user_id, after_seq, limit)
    return [EventOut.model_validate(e.to_wire()) for e in events]


@router.get("/inbox/summary", response_model=InboxSummaryOut)
async def inbox_summary(_user: CurrentUserId) -> InboxSummaryOut:
    # Escalated questions and final-verification items arrive in Milestone 4/5.
    return InboxSummaryOut(open_questions=0, awaiting_verification=0, total=0)
