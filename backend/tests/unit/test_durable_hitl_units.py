"""Tracks 1 + 2 units: global browser pool, Redis frame store, remote-session hub, input event
validation and WebSocket origin checks."""

from __future__ import annotations

import asyncio
import json
import uuid

import fakeredis
import pytest

from app.api.v1.hitl import origin_allowed, parse_input
from app.domain.ports.hitl import TEACH, HitlSession
from app.infrastructure.browser.live_frames import RedisLiveFrameStore
from app.infrastructure.browser.remote_hub import RedisRemoteBrowserHub
from app.infrastructure.runtime.browser_slots import RedisBrowserSlots


def _redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


async def test_browser_pool_caps_concurrency_across_processes() -> None:
    redis = _redis()
    # Two "processes" (two pool objects) share one Redis: the cap is global.
    pools = [RedisBrowserSlots(redis, {"auto": 3}, poll_s=0.01) for _ in range(2)]
    running, peak = 0, 0

    async def browser(i: int) -> None:
        nonlocal running, peak
        async with pools[i % 2].slot("auto"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.03)
            running -= 1

    await asyncio.wait_for(asyncio.gather(*(browser(i) for i in range(10))), timeout=10)
    assert peak == 3
    assert await pools[0].in_use("auto") == 0  # every slot released


async def test_a_crashed_holder_frees_its_slot_after_the_ttl() -> None:
    redis = _redis()
    pool = RedisBrowserSlots(redis, {"hitl": 1}, ttl_s=0.2, poll_s=0.02)
    # A process died holding the only slot: its member stays until its score (expiry) passes.
    await redis.zadd("browser-slots:hitl", {"dead": __import__("time").time() + 0.2})
    async with asyncio.timeout(3):
        async with pool.slot("hitl"):
            assert await pool.in_use("hitl") == 1


async def test_pools_are_independent() -> None:
    pool = RedisBrowserSlots(_redis(), {"auto": 1, "hitl": 1}, poll_s=0.01)
    async with pool.slot("auto"), asyncio.timeout(1), pool.slot("hitl"):
        # a person is never stuck behind automated browsers
        assert await pool.in_use("hitl") == 1


async def test_redis_live_frames_are_per_user_and_expire() -> None:
    store = RedisLiveFrameStore(_redis(), ttl_s=60)
    alice, bob, app = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seq = await store.put(alice, app, b"\xff\xd8one\nwith newline")
    assert await store.get(alice, app) == (seq, b"\xff\xd8one\nwith newline")
    assert await store.get(bob, app) is None
    assert (await store.put(alice, app, b"two")) > seq


def _session(user_id: uuid.UUID) -> HitlSession:
    return HitlSession(uuid.uuid4(), user_id, TEACH, "Programme", "https://p.edu", uuid.uuid4())


async def test_hub_registry_is_per_tenant_and_relays_frames_and_input() -> None:
    hub = RedisRemoteBrowserHub(_redis())
    alice, bob = uuid.uuid4(), uuid.uuid4()
    s = _session(alice)
    assert HitlSession.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    await hub.open(s)
    assert [x.id for x in await hub.list(alice)] == [s.id]
    assert await hub.get(bob, s.id) is None and await hub.list(bob) == []
    assert not await hub.send_input(bob, s.id, {"type": "finish"})  # never another tenant's

    async with hub.frames(alice, s.id) as frames, hub.inputs(s.id) as inputs:
        await hub.publish_frame(s.id, b"\xff\xd8frame")
        assert await frames.get(timeout=2) == b"\xff\xd8frame"
        assert await hub.latest_frame(alice, s.id) == b"\xff\xd8frame"
        assert await hub.latest_frame(bob, s.id) is None
        assert await hub.send_input(alice, s.id, {"type": "key", "action": "down", "key": "a"})
        assert await inputs.get(timeout=2) == {"type": "key", "action": "down", "key": "a"}
        await hub.close(alice, s.id)
        assert await frames.get(timeout=2) == b""  # the "session ended" marker
    assert await hub.get(alice, s.id) is None
    assert not await hub.send_input(alice, s.id, {"type": "finish"})


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ('{"type": "mouse", "action": "click", "x": 10, "y": 20}', True),
        ('{"type": "mouse", "action": "wheel", "x": 1, "y": 1, "dy": 300}', True),
        ('{"type": "key", "action": "down", "key": "Enter"}', True),
        ('{"type": "text", "text": "hello"}', True),
        ('{"type": "nav", "action": "back"}', True),
        ('{"type": "finish"}', True),
        ('{"type": "mouse", "action": "click", "x": 99999, "y": 20}', False),  # off-screen
        ('{"type": "key", "action": "down", "key": "' + "a" * 40 + '"}', False),
        ('{"type": "nav", "action": "goto", "url": "http://169.254.169.254/"}', False),
        ('{"type": "eval", "js": "alert(1)"}', False),
        ("not json", False),
        ('{"type": "text", "text": "' + "x" * 9_000 + '"}', False),  # oversized
    ],
)
def test_input_events_are_validated(raw: str, ok: bool) -> None:
    assert (parse_input(raw) is not None) is ok


def test_websocket_origin_check() -> None:
    allowed = ["http://localhost:3000", "https://console.example.org/"]
    assert origin_allowed("http://localhost:3000", allowed)
    assert origin_allowed("https://console.example.org", allowed)
    assert not origin_allowed("https://evil.example", allowed)
    assert origin_allowed(None, allowed)  # not a browser: cannot carry the cookies cross-site


def test_migration_runner_uses_a_plain_asyncpg_dsn() -> None:
    from app.migrate import asyncpg_dsn

    assert asyncpg_dsn("postgresql+asyncpg://o:p@db:5432/x") == "postgresql://o:p@db:5432/x"
    assert asyncpg_dsn("postgresql://o:p@db/x") == "postgresql://o:p@db/x"
    # Azure Database for PostgreSQL: SQLAlchemy's `ssl` keyword becomes libpq's `sslmode`.
    assert (
        asyncpg_dsn("postgresql+asyncpg://o:p@h.postgres.database.azure.com:5432/x?ssl=require")
        == "postgresql://o:p@h.postgres.database.azure.com:5432/x?sslmode=require"
    )
