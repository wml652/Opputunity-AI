from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import register

NIM_KEY_A = "nvapi-ALICE-secret-key-0123456789"


async def test_user_b_cannot_touch_user_a_llm_configs(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")

    created = await alice.post(
        "/api/v1/llm/providers",
        json={"kind": "NVIDIA_NIM", "model": "test/chat-model", "api_key": NIM_KEY_A},
    )
    assert created.status_code == 201
    config_id = created.json()["id"]

    assert (await bob.get("/api/v1/llm/providers")).json() == []
    assert (await bob.get("/api/v1/llm/status")).json() == {"configured": False, "provider": None}
    assert (
        await bob.patch(f"/api/v1/llm/providers/{config_id}", json={"label": "x"})
    ).status_code == 404
    assert (await bob.post(f"/api/v1/llm/providers/{config_id}/activate")).status_code == 404
    assert (await bob.delete(f"/api/v1/llm/providers/{config_id}")).status_code == 404
    verify = await bob.post("/api/v1/llm/verify", json={"provider_config_id": config_id})
    assert verify.status_code == 404
    assert (await bob.get("/api/v1/events")).json() == []

    # Alice still owns it, untouched.
    mine = (await alice.get("/api/v1/llm/providers")).json()
    assert [c["id"] for c in mine] == [config_id] and mine[0]["label"] == "NVIDIA NIM"


async def _seed_two_tenants(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    a, b = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        for uid in (a, b):
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, display_name) "
                    "VALUES (:id, :email, 'x', 'x')"
                ),
                {"id": uid, "email": f"{uid}@example.com"},
            )
        for uid in (a, b):
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid)})
            await conn.execute(
                text("INSERT INTO pipeline_events (id, user_id, type) VALUES (:id, :uid, 'seed')"),
                {"id": uuid.uuid4(), "uid": uid},
            )
    return a, b


async def test_rls_filters_rows_at_the_database(engine: AsyncEngine) -> None:
    a, b = await _seed_two_tenants(engine)
    async with engine.begin() as conn:
        # No tenant bound: the app role sees nothing.
        assert (await conn.execute(text("SELECT count(*) FROM pipeline_events"))).scalar() == 0
        await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(b)})
        rows = (await conn.execute(text("SELECT user_id FROM pipeline_events"))).scalars().all()
        assert rows == [b]
        # Even an explicit filter for A returns nothing under B's context.
        leaked = await conn.execute(
            text("SELECT count(*) FROM pipeline_events WHERE user_id = :a"), {"a": a}
        )
        assert leaked.scalar() == 0


async def test_rls_blocks_cross_tenant_writes(engine: AsyncEngine) -> None:
    a, b = await _seed_two_tenants(engine)
    with pytest.raises(DBAPIError, match="row-level security"):  # noqa: PT012
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(b)})
            await conn.execute(
                text("INSERT INTO pipeline_events (id, user_id, type) VALUES (:id, :a, 'forged')"),
                {"id": uuid.uuid4(), "a": a},
            )
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(b)})
        updated = await conn.execute(
            text("UPDATE pipeline_events SET type='hijack' WHERE user_id = :a"), {"a": a}
        )
        assert updated.rowcount == 0


async def test_tenant_setting_does_not_leak_across_transactions(engine: AsyncEngine) -> None:
    _a, b = await _seed_two_tenants(engine)
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(b)})
        async with conn.begin():
            count = await conn.execute(text("SELECT count(*) FROM pipeline_events"))
            assert count.scalar() == 0


async def test_every_tenant_table_has_forced_rls(clean_db) -> None:  # type: ignore[no-untyped-def]
    conn = await asyncpg.connect(clean_db.owner)
    try:
        rows = await conn.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid
                           AND p.polname = 'tenant_isolation') AS has_policy
            FROM pg_class c
            JOIN information_schema.columns col
              ON col.table_name = c.relname AND col.column_name = 'user_id'
            WHERE c.relkind = 'r' AND col.table_schema = 'public'
            """
        )
        app_role_is_super = await conn.fetchval(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'app_rt'"
        )
    finally:
        await conn.close()
    assert rows, "expected tenant tables"
    for row in rows:
        assert row["relrowsecurity"] and row["relforcerowsecurity"] and row["has_policy"], row
    assert app_role_is_super is False
