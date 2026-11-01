"""`uv run migrate` - apply Alembic migrations, safe to run from every replica at startup.

Containers start in parallel (Azure Container Apps scales the API to several replicas); a
Postgres advisory lock makes exactly one of them migrate while the others wait, then find
nothing to do. Runs as the OWNER role (DATABASE_MIGRATION_URL); the app role never migrates."""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from app.infrastructure.config.settings import get_settings
from app.infrastructure.runtime.event_loop import configure_event_loop, run

MIGRATION_LOCK_KEY = 7_340_011  # arbitrary, constant across releases


def asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URL (postgresql+asyncpg://) -> plain asyncpg DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _migrate() -> int:
    settings = get_settings()
    url = settings.database_migration_url or settings.database_url
    conn = await asyncpg.connect(asyncpg_dsn(url))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        # A separate process: Alembic's env.py runs its own event loop.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "upgrade", "head"
        )
        return await proc.wait()
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        finally:
            await conn.close()


def main() -> None:
    configure_event_loop()
    sys.exit(run(_migrate()))


if __name__ == "__main__":
    main()
