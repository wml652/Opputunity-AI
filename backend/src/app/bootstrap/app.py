from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.app_factory import build_api
from app.bootstrap.container import (
    Infrastructure,
    build_api_container,
    build_infrastructure,
    build_maintenance,
    build_relay,
    build_sweep,
)
from app.infrastructure.config.settings import Settings, get_settings
from app.infrastructure.events.outbox_relay import OutboxRelayLoop
from app.infrastructure.logging import configure_logging, get_logger
from app.infrastructure.runtime.event_loop import configure_event_loop

log = get_logger(__name__)


def create_app_with(
    infra: Infrastructure, *, run_relay: bool | None = None, run_sweep: bool = True
) -> FastAPI:
    settings = infra.settings
    relay_enabled = settings.outbox_relay_in_api if run_relay is None else run_relay

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        relay_loop: OutboxRelayLoop | None = None
        if relay_enabled:
            relay_loop = OutboxRelayLoop(
                build_relay(infra),
                database_url=settings.database_url,
                poll_interval=settings.outbox_poll_interval_seconds,
            )
            relay_loop.start()
        maintenance: asyncio.Task[None] | None = None
        if settings.campaign_runner == "inprocess" and run_sweep:
            interrupted = await build_sweep(infra).execute()
            if interrupted:
                log.warning("campaigns.interrupted_marked_failed", count=interrupted)
            maintenance = asyncio.create_task(_maintenance_loop(infra))
        log.info("api.started", environment=settings.environment, outbox_relay=relay_enabled)
        if settings.dev_auth_bypass:
            log.warning("api.dev_auth_bypass_enabled", detail="login is disabled (DEV ONLY)")
        try:
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                with contextlib.suppress(BaseException):
                    await maintenance
            if relay_loop is not None:
                await relay_loop.stop()
            await infra.aclose()

    return build_api(build_api_container(infra), lifespan=lifespan)


async def _maintenance_loop(infra: Infrastructure) -> None:
    """In-process mode: lease reaper + deadline sweep at startup, then every few minutes.
    (With CAMPAIGN_RUNNER=arq the worker runs the same jobs as cron tasks.)"""
    reaper, deadlines = build_maintenance(infra)
    while True:
        try:
            reaped = await reaper.execute()
            expired = await deadlines.execute()
            if reaped or expired:
                log.info("applications.maintenance", reaped=reaped, expired=expired)
        except Exception:
            log.exception("applications.maintenance_failed")
        await asyncio.sleep(infra.settings.maintenance_interval_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    """ASGI factory: `uvicorn app.bootstrap.app:create_app --factory`."""
    configure_event_loop()
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    return create_app_with(build_infrastructure(settings))
