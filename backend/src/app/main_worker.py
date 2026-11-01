"""`uv run worker [--queue system]` — arq worker entrypoint (Windows-safe).

The bare `arq` CLI is not used: it installs POSIX signal handlers (unsupported on Windows) and
creates its own loop. Here we pin a Proactor loop (Playwright needs subprocess support) and run
`Worker.async_run()` inside it."""

from __future__ import annotations

import argparse
import contextlib
import socket
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import Worker

from app.application.applications.submission import RunSubmission
from app.application.applications.use_cases import RunApplication
from app.application.discovery.use_cases import RunCampaign
from app.bootstrap.container import (
    build_application_deps,
    build_discovery_deps,
    build_infrastructure,
    build_maintenance,
    build_relay,
    build_resume,
    make_runner_id,
)
from app.infrastructure.config.settings import get_settings
from app.infrastructure.events.outbox_relay import OutboxRelayLoop
from app.infrastructure.logging import configure_logging, get_logger
from app.infrastructure.runtime.event_loop import IS_WINDOWS, configure_event_loop, run
from app.infrastructure.runtime.resources import PsutilResources
from app.infrastructure.workers.tasks import (
    QUEUES,
    heartbeat,
    maintenance,
    make_worker_id,
    ping,
    relay_outbox,
    resume_work,
    run_application,
    run_campaign,
    run_submission,
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
    infra.runner_id = make_runner_id(queue_name)  # identity in run leases
    relay = build_relay(infra)
    reap_leases, sweep_deadlines = build_maintenance(infra)
    relay_loop: OutboxRelayLoop | None = None

    async def on_startup(ctx: dict[str, Any]) -> None:
        nonlocal relay_loop
        app_deps = build_application_deps(infra)
        ctx.update(
            run_campaign=RunCampaign(build_discovery_deps(infra)),
            run_application=RunApplication(app_deps),
            run_submission=RunSubmission(app_deps),
            resume=build_resume(infra),
            browser_slots=infra.browser_slots,
            resources=PsutilResources(),
            min_free_memory_mb=settings.min_free_memory_mb,
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
        # Durable execution: resume runs whose worker died, every minute (and at startup).
        cron_jobs.append(cron(resume_work, second={20}, run_at_startup=True))

    return Worker(
        functions=[
            ping,
            relay_outbox,
            run_campaign,
            run_application,
            run_submission,
            maintenance,
            resume_work,
        ],
        cron_jobs=cron_jobs,
        queue_name=f"arq:{queue_name}",
        redis_settings=RedisSettings.from_dsn(settings.redis_url),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        handle_signals=not IS_WINDOWS,
        # Browser jobs also wait for a slot in the global pool (MAX_BROWSERS across processes).
        max_jobs=settings.max_browsers if queue_name == "browser" else 10,
        # A campaign can run for hours; arq re-runs a job whose worker vanished only after
        # this, but the resume sweep (run leases) normally restarts it within ~2 minutes.
        job_timeout=6 * 3600,
        health_check_interval=30,
    )


async def _check(queue_name: str) -> bool:
    """Container health check: a worker of this queue on this host sent a heartbeat within
    its TTL (WORKER_HEARTBEAT_TTL_SECONDS)."""
    from redis.asyncio import Redis

    redis = Redis.from_url(get_settings().redis_url)
    try:
        prefix = f"workers:heartbeat:{socket.gethostname()}:"
        async for key in redis.scan_iter(match=f"{prefix}*:{queue_name}", count=100):
            return bool(key)
        return False
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an arq worker")
    parser.add_argument("--queue", choices=QUEUES, default="system")
    parser.add_argument(
        "--check", action="store_true", help="exit 0 if this host's worker is alive"
    )
    args = parser.parse_args()

    configure_event_loop()
    if args.check:
        raise SystemExit(0 if run(_check(args.queue)) else 1)
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
