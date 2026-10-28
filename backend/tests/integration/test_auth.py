from __future__ import annotations

import asyncpg
import httpx
import pytest

from tests.conftest import DatabaseUrls, register


async def test_register_sets_cookies_and_me_works(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "Bob@Example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"]["email"] == "bob@example.com"
    assert "password" not in resp.text and "refresh" not in body
    set_cookie = "; ".join(resp.headers.get_list("set-cookie")).lower()
    assert "access_token=" in set_cookie and "refresh_token=" in set_cookie
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert resp.headers["cache-control"] == "no-store"

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "bob@example.com"


async def test_duplicate_email_conflicts(client: httpx.AsyncClient) -> None:
    await register(client, "dup@example.com")
    client.cookies.clear()
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "DUP@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 409


async def test_login_rejects_bad_password_without_leaking(client: httpx.AsyncClient) -> None:
    await register(client, "carol@example.com")
    client.cookies.clear()
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "carol@example.com", "password": "nope-nope-nope"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()  # no account enumeration
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": "carol@example.com", "password": "correct-horse-battery"},
    )
    assert ok.status_code == 200


async def test_unauthenticated_requests_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.get("/api/v1/llm/providers")).status_code == 401
    bad = await client.get("/api/v1/auth/me", headers={"authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401


async def test_refresh_rotates_and_reuse_revokes_family(
    client: httpx.AsyncClient, clean_db: DatabaseUrls
) -> None:
    alice = await register(client, "alice@example.com")
    original = client.cookies.get("refresh_token")
    assert original

    rotated = await client.post("/api/v1/auth/refresh", headers={"x-csrf-token": alice.csrf})
    assert rotated.status_code == 200
    new_refresh = client.cookies.get("refresh_token")
    new_csrf = rotated.json()["csrf_token"]
    assert new_refresh and new_refresh != original

    # Attacker replays the stolen, already-rotated token.
    client.cookies.set("refresh_token", original, path="/api/v1/auth")
    reuse = await client.post("/api/v1/auth/refresh", headers={"x-csrf-token": new_csrf})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "refresh_token_reuse"

    # The legitimate (newest) token in that family is now dead too.
    client.cookies.set("refresh_token", new_refresh, path="/api/v1/auth")
    after = await client.post("/api/v1/auth/refresh", headers={"x-csrf-token": new_csrf})
    assert after.status_code == 401

    conn = await asyncpg.connect(clean_db.owner)
    try:
        live = await conn.fetchval(
            "SELECT count(*) FROM refresh_tokens WHERE user_id=$1 AND revoked_at IS NULL", alice.id
        )
        audit = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action='auth.refresh_reuse_detected'"
        )
    finally:
        await conn.close()
    # Both the replayed token and the (now revoked) newest token are reported as reuse.
    assert live == 0 and audit == 2


async def test_refresh_token_stored_only_as_hash(
    client: httpx.AsyncClient, clean_db: DatabaseUrls
) -> None:
    await register(client, "hash@example.com")
    raw = client.cookies.get("refresh_token")
    assert raw
    secret = raw.split(".")[2]
    conn = await asyncpg.connect(clean_db.owner)
    try:
        rows = await conn.fetch("SELECT token_hash FROM refresh_tokens")
    finally:
        await conn.close()
    assert rows and all(secret not in r["token_hash"] for r in rows)


async def test_logout_revokes_session(client: httpx.AsyncClient) -> None:
    alice = await register(client, "out@example.com")
    raw = client.cookies.get("refresh_token")
    resp = await client.post("/api/v1/auth/logout", headers={"x-csrf-token": alice.csrf})
    assert resp.status_code == 204
    client.cookies.set("refresh_token", raw or "", path="/api/v1/auth")
    client.cookies.set("csrf_token", alice.csrf)
    again = await client.post("/api/v1/auth/refresh", headers={"x-csrf-token": alice.csrf})
    assert again.status_code == 401


async def test_csrf_required_for_cookie_auth(client: httpx.AsyncClient) -> None:
    alice = await register(client, "csrf@example.com")
    payload = {
        "kind": "NVIDIA_NIM",
        "model": "test/chat-model",
        "api_key": "nvapi-test-key-000000000000",
    }
    missing = await client.post("/api/v1/llm/providers", json=payload)
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf_failed"
    wrong = await client.post(
        "/api/v1/llm/providers", json=payload, headers={"x-csrf-token": "forged"}
    )
    assert wrong.status_code == 403
    ok = await client.post(
        "/api/v1/llm/providers", json=payload, headers={"x-csrf-token": alice.csrf}
    )
    assert ok.status_code == 201


async def test_bearer_clients_skip_csrf(client: httpx.AsyncClient) -> None:
    await register(client, "bearer@example.com")
    token = client.cookies.get("access_token")
    client.cookies.clear()
    resp = await client.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": "test/chat-model",
            "api_key": "nvapi-test-key-000000000000",
        },
        headers={"authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-request-id"]


async def test_dev_auth_bypass_skips_login(infra) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from app.bootstrap.app import create_app_with

    app = create_app_with(infra, run_relay=False)
    container = app.state.container
    app.state.container = replace(container, config=replace(container.config, dev_auth_bypass=True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anon:
        me = await anon.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "dev@opportunity.local"
        # No cookies => no CSRF requirement; the dev user can configure providers.
        created = await anon.post(
            "/api/v1/llm/providers",
            json={
                "kind": "NVIDIA_NIM",
                "model": "test/chat-model",
                "api_key": "nvapi-test-key-000000000000",
            },
        )
        assert created.status_code == 201
        assert (await anon.get("/api/v1/auth/me")).json()["id"] == me.json()["id"]


def test_dev_auth_bypass_refused_in_production() -> None:
    from app.infrastructure.config.settings import Settings

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        dev_auth_bypass=True,
        encryption_keys="1:" + "A" * 43 + "=",  # type: ignore[arg-type]
        jwt_secret="x" * 48,  # type: ignore[arg-type]
        cookie_secure=True,
    )
    with pytest.raises(RuntimeError, match="DEV_AUTH_BYPASS"):
        s.validate_for_runtime()
