"""Shared fixtures.

Integration tests run against a real PostgreSQL: `TEST_DATABASE_URL` (owner/superuser DSN) if set,
otherwise an embedded server from `pgserver` (bundles pgvector; works natively on Windows).
The application connects as a separate NON-superuser role so row-level security is enforced
exactly as in production. Redis is `fakeredis` unless `TEST_REDIS_URL` is set."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import fakeredis
import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.infrastructure.runtime.event_loop import configure_event_loop

configure_event_loop()
# pgserver logs from an atexit hook after pytest has closed the captured streams.
logging.raiseExceptions = False

from app.bootstrap.app import create_app_with  # noqa: E402
from app.bootstrap.container import Infrastructure, build_infrastructure  # noqa: E402
from app.infrastructure.config.settings import Settings  # noqa: E402
from tests.fixtures import ControlledEmbeddingFactory  # noqa: E402
from tests.portal import Portal  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROLE = "app_rt"
APP_ROLE_PASSWORD = "app_rt_test_password"
TEST_DB = "app_test"

KEY_V1 = base64.b64encode(b"\x01" * 32).decode()
KEY_V2 = base64.b64encode(b"\x02" * 32).decode()


@dataclass(frozen=True)
class DatabaseUrls:
    owner: str  # postgresql://… (asyncpg DSN) — superuser/owner, used for migrations & cleanup
    app: str  # postgresql+asyncpg://… — non-superuser app role (RLS enforced)


def _with_db(dsn: str, database: str) -> str:
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{database}"


def _as_role(dsn: str, role: str, password: str) -> str:
    scheme, rest = dsn.split("://", 1)
    hostpart = rest.split("@", 1)[1] if "@" in rest else rest
    return f"{scheme}://{role}:{password}@{hostpart}"


@pytest.fixture(scope="session")
def database_urls() -> Iterator[DatabaseUrls]:
    from app.infrastructure.runtime.event_loop import run

    data_dir: str | None = None
    server: Any = None
    admin_dsn = os.environ.get("TEST_DATABASE_URL", "").replace("+asyncpg", "")
    if not admin_dsn:
        import pgserver

        data_dir = tempfile.mkdtemp(prefix="pgtest-")
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        admin_dsn = server.get_uri()

    async def prepare() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            await conn.execute(f"CREATE DATABASE {TEST_DB}")
            exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE)
            if not exists:
                await conn.execute(
                    f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS "
                    f"PASSWORD '{APP_ROLE_PASSWORD}'"
                )
        finally:
            await conn.close()

    run(prepare())
    owner = _with_db(admin_dsn, TEST_DB)

    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    cfg.attributes["database_url"] = owner.replace("postgresql://", "postgresql+asyncpg://", 1)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

    async def grant() -> None:
        conn = await asyncpg.connect(owner)
        try:
            await conn.execute(
                f"""
                GRANT USAGE ON SCHEMA public TO {APP_ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
                """
            )
        finally:
            await conn.close()

    run(grant())
    app_url = _as_role(owner, APP_ROLE, APP_ROLE_PASSWORD).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    yield DatabaseUrls(owner=owner, app=app_url)

    if server is not None:
        server.cleanup()
    if data_dir:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def portal() -> Iterator[Portal]:
    server = Portal().start()
    yield server
    server.stop()


@pytest.fixture
def settings(tmp_path: Path, portal: Portal) -> Settings:
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        scraper_test_fixture_hosts=[portal.host],
        crawl_use_playwright=False,
        crawl_politeness_seconds=0.0,
        _env_file=None,  # type: ignore[call-arg]  # tests never read the developer's .env
        environment="test",
        database_url="postgresql+asyncpg://unused/unused",
        encryption_keys=f"1:{KEY_V1}",  # type: ignore[arg-type]
        jwt_secret="test-jwt-secret-that-is-long-enough-1234567890",  # type: ignore[arg-type]
        argon2_time_cost=1,
        argon2_memory_cost_kib=1024,
        argon2_parallelism=1,
        llm_timeout_seconds=2.0,
        sse_heartbeat_seconds=0.5,
        outbox_relay_in_api=False,
    )


@pytest.fixture
async def clean_db(database_urls: DatabaseUrls) -> DatabaseUrls:
    conn = await asyncpg.connect(database_urls.owner)
    try:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    finally:
        await conn.close()
    return database_urls


@pytest.fixture
async def engine(clean_db: DatabaseUrls) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(clean_db.app, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    client: Redis = Redis.from_url(url) if url else fakeredis.aioredis.FakeRedis()
    await client.flushdb()
    yield client
    await client.aclose()


async def public_resolver(host: str, port: int) -> list[str]:
    """Deterministic DNS for tests: `*.internal-test` -> private, everything else -> public."""
    if host.endswith("internal-test"):
        return ["10.0.0.5"]
    if host.startswith("rebind"):
        return ["93.184.216.34", "169.254.169.254"]
    return ["93.184.216.34"]


@pytest.fixture
def embedding_factory() -> ControlledEmbeddingFactory:
    return ControlledEmbeddingFactory()


@pytest.fixture
async def infra(
    settings: Settings,
    engine: AsyncEngine,
    redis: Redis,
    clean_db: DatabaseUrls,
    embedding_factory: ControlledEmbeddingFactory,
) -> AsyncIterator[Infrastructure]:
    settings = settings.model_copy(update={"database_url": clean_db.app})
    built = build_infrastructure(settings, redis=redis, engine=engine, resolver=public_resolver)
    built.embedding_factory = embedding_factory  # type: ignore[assignment]
    yield built
    await built.llm_factory.aclose()


@pytest.fixture
async def client(infra: Infrastructure) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with(infra, run_relay=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


class ApiUser:
    """A logged-in test client wrapper that adds the CSRF header to mutating requests."""

    def __init__(self, client: httpx.AsyncClient, csrf: str, user_id: uuid.UUID) -> None:
        self.client = client
        self.csrf = csrf
        self.id = user_id

    def _h(self) -> dict[str, str]:
        return {"x-csrf-token": self.csrf}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.post(url, headers=self._h(), **kw)

    async def patch(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.patch(url, headers=self._h(), **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self._h(), **kw)


async def register(client: httpx.AsyncClient, email: str) -> ApiUser:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "display_name": email[:5]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return ApiUser(client, body["csrf_token"], uuid.UUID(body["user"]["id"]))


@pytest.fixture
async def user(client: httpx.AsyncClient) -> ApiUser:
    return await register(client, "alice@example.com")


@pytest.fixture
async def other_client(infra: Infrastructure) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
