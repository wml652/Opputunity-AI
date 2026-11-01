"""`migrate` - apply Alembic migrations, safe to run from every replica at startup.

Containers start in parallel (Azure Container Apps can run several API replicas); a Postgres
advisory lock makes exactly one of them migrate while the others wait, then find nothing to
do. Runs as the OWNER role (DATABASE_MIGRATION_URL).

When the app connects as a different role (DATABASE_URL), that role is created if it is
missing - LOGIN NOSUPERUSER NOBYPASSRLS, so row-level security applies to it - and granted
DML on everything the owner creates. Managed Postgres (Azure Flexible Server) gives the admin
CREATEROLE but no init-script hook, so this is where the app role comes from there."""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import unquote, urlsplit

import asyncpg

from app.infrastructure.config.settings import get_settings
from app.infrastructure.db.session import asyncpg_dsn
from app.infrastructure.runtime.event_loop import configure_event_loop, run

MIGRATION_LOCK_KEY = 7_340_011  # arbitrary, constant across releases

__all__ = ["asyncpg_dsn", "main", "role_of"]


def role_of(url: str) -> tuple[str, str]:
    """(user, password) from a database URL."""
    parts = urlsplit(asyncpg_dsn(url))
    return unquote(parts.username or ""), unquote(parts.password or "")


async def _ensure_app_role(conn: asyncpg.Connection, app_url: str, owner: str) -> None:
    user, password = role_of(app_url)
    if not user or user == owner:
        return  # the app uses the owner role itself (local dev): nothing to create
    if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", user):
        # DDL takes no bind parameters: let the server quote identifier and literal (%I / %L).
        ddl = await conn.fetchval(
            "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', "
            "$1::text, $2::text)",
            user,
            password,
        )
        await conn.execute(ddl)
    statements = [
        "GRANT USAGE ON SCHEMA public TO %I",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I",
    ]
    for template in statements:
        await conn.execute(await conn.fetchval("SELECT format($1::text, $2::text)", template, user))


async def _migrate() -> int:
    settings = get_settings()
    owner_url = settings.database_migration_url or settings.database_url
    owner, _ = role_of(owner_url)
    conn = await asyncpg.connect(asyncpg_dsn(owner_url))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        # A separate process: Alembic's env.py runs its own event loop.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "upgrade", "head"
        )
        code = await proc.wait()
        if code == 0:
            await _ensure_app_role(conn, settings.database_url, owner)
        return code
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
