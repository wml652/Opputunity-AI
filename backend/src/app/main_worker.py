"""`uv run worker [--queue system]` — arq worker entrypoint (Windows-safe).

The bare `arq` CLI is not used: it installs POSIX signal handlers (unsupported on Windows) and
creates its own loop. Here we pin a Proactor loop (Playwright needs subprocess support) and run
`Worker.async_run()` inside it."""

from __future__ import annotations

import argparse
import contextlib
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import Worker

from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.container import (
    build_discovery_deps,
    build_infrastructure,
    build_maintenance,
    build_relay,
)
from app.infrastructure.config.settings import get_settings
from app.infrastructure.events.outbox_relay import OutboxRelayLoop
from app.infrastructure.logging import configure_logging, get_logger
from app.infrastructure.runtime.event_loop import IS_WINDOWS, configure_event_loop, run
from app.infrastructure.workers.tasks import (
    QUEUES,
    heartbeat,
    maintenance,
    make_worker_id,
    ping,
    relay_outbox,
    run_campaign,
)

log = get_logger(__name__)

_HEARTBEAT_SECONDS = {0, 10, 20, 30, 40, 50}


def build_worker(queue_name: str) -> Worker:
    settings = get_settings()
    if settings.redis_url.startswith("memory://"):
        raise SystemExit(
            "REDIS_URL=memory:// runs everything inside the API process; workers need a real "
            "Redis (Upstash rediss://... or Memurai on Windows)."
        )
    infra = build_infrastructure(settings)
    relay = build_relay(infra)
    reap_leases, sweep_deadlines = build_maintenance(infra)
    relay_loop: OutboxRelayLoop | None = None

    async def on_startup(ctx: dict[str, Any]) -> None:
        nonlocal relay_loop
        ctx.update(
            run_campaign=RunCampaign(build_discovery_deps(infra)),
            relay=relay,
            reap_leases=reap_leases,
            sweep_deadlines=sweep_deadlines,
            registry=infra.worker_registry,
            worker_id=make_worker_id(queue_name),
            queue_name=queue_name,
            heartbeat_ttl=settings.worker_heartbeat_ttl_seconds,
        )
        await heartbeat(ctx)
        if queue_name == "system":
            relay_loop = OutboxRelayLoop(
                relay,
                database_url=settings.database_url,
                poll_interval=settings.outbox_poll_interval_seconds,
            )
            relay_loop.start()
        log.info("worker.started", queue=queue_name, worker_id=ctx["worker_id"])

    async def on_shutdown(ctx: dict[str, Any]) -> None:
        if relay_loop is not None:
            await relay_loop.stop()
        await infra.aclose()
        log.info("worker.stopped", queue=queue_name)

    cron_jobs = [cron(heartbeat, second=_HEARTBEAT_SECONDS, run_at_startup=False)]
    if queue_name == "system":
        cron_jobs.append(cron(relay_outbox, second={5, 35}, run_at_startup=False))
        cron_jobs.append(cron(maintenance, minute=set(range(0, 60, 5)), run_at_startup=True))

    return Worker(
        functions=[ping, relay_outbox, run_campaign, maintenance],
        cron_jobs=cron_jobs,
        queue_name=f"arq:{queue_name}",
        redis_settings=RedisSettings.from_dsn(settings.redis_url),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        handle_signals=not IS_WINDOWS,
        max_jobs=4 if queue_name == "browser" else 10,
        health_check_interval=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an arq worker")
    parser.add_argument("--queue", choices=QUEUES, default="system")
    args = parser.parse_args()

    configure_event_loop()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    async def _main() -> None:
        worker = build_worker(args.queue)
        try:
            await worker.async_run()
        finally:
            await worker.close()

    with contextlib.suppress(KeyboardInterrupt):
        run(_main())


if __name__ == "__main__":
    main()
