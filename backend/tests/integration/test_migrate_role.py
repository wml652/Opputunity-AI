"""`migrate` creates the non-superuser app role on managed Postgres (no init-script hook)."""

from __future__ import annotations

import asyncpg

from app.migrate import _ensure_app_role, role_of
from tests.conftest import DatabaseUrls

ROLE = "ootg_migrate_test_app"


async def test_app_role_is_created_without_rls_bypass_and_granted(clean_db: DatabaseUrls) -> None:
    conn = await asyncpg.connect(clean_db.owner)
    owner = await conn.fetchval("SELECT current_user")
    app_url = f"postgresql+asyncpg://{ROLE}:p%40ss'word@db:5432/x?ssl=require"
    assert role_of(app_url) == (ROLE, "p@ss'word")
    try:
        await _ensure_app_role(conn, app_url, owner)
        await _ensure_app_role(conn, app_url, owner)  # idempotent
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = $1", ROLE
        )
        assert row is not None and row["rolcanlogin"]
        assert not row["rolsuper"] and not row["rolbypassrls"]  # RLS applies to the app
        assert await conn.fetchval(
            "SELECT has_table_privilege($1, 'applications', 'SELECT, INSERT, UPDATE, DELETE')",
            ROLE,
        )
        # The app uses the owner role itself (local dev): nothing is created.
        await _ensure_app_role(conn, f"postgresql+asyncpg://{owner}:x@db/x", owner)
    finally:
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await conn.execute(f'DROP OWNED BY "{ROLE}"')
        await conn.execute(f'DROP ROLE IF EXISTS "{ROLE}"')
        await conn.close()
