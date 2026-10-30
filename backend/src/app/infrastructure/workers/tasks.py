"""arq task functions. Dependencies are placed in the arq `ctx` by the composition root
(`app.main_worker`) at startup; tasks only talk to application use cases and ports."""

from __future__ import annotations

import os
import platform
import socket
import uuid
from typing import Any, TypedDict

from app.application.discovery.use_cases import RunCampaign
from app.application.events.use_cases import RelayOutbox
from app.domain.ports.events import IWorkerRegistry
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

QUEUES = ("discovery", "reasoning", "browser", "notify", "system")


class WorkerContext(TypedDict, total=False):
    relay: RelayOutbox
    registry: IWorkerRegistry
    worker_id: str
    queue_name: str
    heartbeat_ttl: int


def make_worker_id(queue_name: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{queue_name}"


async def relay_outbox(ctx: dict[str, Any]) -> int:
    """Fallback drain (the API/worker relay loop normally handles this via LISTEN/NOTIFY)."""
    relay: RelayOutbox = ctx["relay"]
    total = 0
    while (published := await relay.execute()) > 0:
        total += published
    return total


async def heartbeat(ctx: dict[str, Any]) -> None:
    registry: IWorkerRegistry = ctx["registry"]
    await registry.heartbeat(
        ctx["worker_id"],
        {
            "queue": ctx.get("queue_name", "system"),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "platform": platform.system(),
            "jobs_running": len(ctx.get("running_jobs", ())) if "running_jobs" in ctx else None,
        },
        ttl_seconds=int(ctx.get("heartbeat_ttl", 30)),
    )


async def run_campaign(ctx: dict[str, Any], user_id: str, campaign_id: str) -> None:
    run: RunCampaign = ctx["run_campaign"]
    await run.execute(uuid.UUID(user_id), uuid.UUID(campaign_id))


async def ping(ctx: dict[str, Any], message: str = "pong") -> str:
    log.info("worker.ping", worker_id=ctx.get("worker_id"), message=message)
    return message


async def maintenance(ctx: dict[str, Any]) -> dict[str, int]:
    """Cron (system queue): hand interrupted runs back to the user and expire past deadlines."""
    reaper: Any = ctx["reap_leases"]
    deadlines: Any = ctx["sweep_deadlines"]
    return {"reaped": await reaper.execute(), "expired": await deadlines.execute()}
