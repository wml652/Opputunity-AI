"""SSE replay + live streaming through a real uvicorn server (httpx's ASGI transport buffers
whole responses, so streaming must go over a socket)."""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure, build_relay
from app.domain.events.models import NewEvent
from app.domain.identity.models import User
from app.domain.shared.ids import new_uuid7
from app.infrastructure.events.outbox_relay import OutboxRelayLoop


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server_url(infra: Infrastructure) -> AsyncIterator[str]:
    port = _free_port()
    app = create_app_with(infra, run_relay=False)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, loop="none", log_config=None, lifespan="off"
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


async def _session(base: str, email: str) -> tuple[httpx.AsyncClient, str, str]:
    client = httpx.AsyncClient(base_url=base, timeout=10)
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return client, body["csrf_token"], body["user"]["id"]


async def _append(infra: Infrastructure, user_id: str, type_: str, n: int) -> None:
    async with infra.uow_factory(uuid.UUID(user_id)) as uow:
        await uow.events.append(NewEvent(user_id=uuid.UUID(user_id), type=type_, payload={"n": n}))
        await uow.commit()


async def _read_events(
    lines: AsyncIterator[str], count: int, timeout: float = 10.0
) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    comments: list[str] = []

    async def consume() -> None:
        data: str | None = None
        async for line in lines:
            if line.startswith(":"):
                comments.append(line)
            elif line.startswith("data: "):
                data = line[6:]
            elif line == "" and data is not None:
                events.append(json.loads(data))
                data = None
                if len(events) >= count:
                    return

    await asyncio.wait_for(consume(), timeout=timeout)
    return events, comments


async def test_sse_replays_after_last_event_id_then_streams_live(
    server_url: str, infra: Infrastructure
) -> None:
    client, _csrf, user_id = await _session(server_url, "sse@example.com")
    for n in range(1, 4):
        await _append(infra, user_id, "test.event", n)
    all_events = (await client.get("/api/v1/events")).json()
    seqs = [e["seq"] for e in all_events if e["type"] == "test.event"]
    assert len(seqs) == 3

    relay = build_relay(infra)
    async with client.stream(
        "GET", "/api/v1/events/stream", headers={"Last-Event-ID": str(seqs[0])}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "no-cache" in response.headers["cache-control"]

        lines = response.aiter_lines()
        replayed, _ = await _read_events(lines, 2)
        assert [e["seq"] for e in replayed] == seqs[1:]  # only events AFTER Last-Event-ID

        # Live: commit a new event, relay publishes it to Redis, SSE forwards it.
        await _append(infra, user_id, "test.live", 99)
        assert await relay.execute() >= 1
        live, comments = await _read_events(lines, 1)
        assert live[0]["type"] == "test.live" and live[0]["payload"] == {"n": 99}
        assert live[0]["seq"] > seqs[-1]

        # Re-publishing an already-delivered event is deduplicated by seq; heartbeats flow.
        await infra.event_bus.publish(uuid.UUID(user_id), {**replayed[0], "type": "dup"})
        await asyncio.sleep(0.8)
        await _append(infra, user_id, "test.after", 100)
        await relay.execute()
        nxt, comments2 = await _read_events(lines, 1)
        assert nxt[0]["type"] == "test.after"
        assert any("heartbeat" in c for c in comments + comments2)
    await client.aclose()


async def test_sse_is_tenant_scoped(server_url: str, infra: Infrastructure) -> None:
    alice, _, alice_id = await _session(server_url, "a-sse@example.com")
    bob, _, bob_id = await _session(server_url, "b-sse@example.com")
    await _append(infra, alice_id, "alice.secret", 1)
    await _append(infra, bob_id, "bob.visible", 2)
    async with bob.stream("GET", "/api/v1/events/stream") as response:
        events, _ = await _read_events(response.aiter_lines(), 1)
    assert [e["type"] for e in events] == ["bob.visible"]
    await alice.aclose()
    await bob.aclose()


async def test_sse_requires_auth(server_url: str) -> None:
    async with httpx.AsyncClient(base_url=server_url) as anon:
        resp = await anon.get("/api/v1/events/stream")
    assert resp.status_code == 401


async def test_outbox_relay_loop_listen_notify(infra: Infrastructure) -> None:
    """The relay wakes on Postgres NOTIFY and publishes to the user's Redis channel."""
    async with infra.uow_factory() as uow:
        uid = new_uuid7()
        await uow.users.add(
            User(
                id=uid,
                email="relay@example.com",
                password_hash="x",
                display_name="r",
                timezone="UTC",
                is_active=True,
                created_at=infra.clock.now(),
            )
        )
        await uow.commit()

    loop = OutboxRelayLoop(
        build_relay(infra), database_url=infra.settings.database_url, poll_interval=30.0
    )
    async with infra.event_bus.subscribe(uid) as sub:
        loop.start()
        await asyncio.sleep(0.5)  # let LISTEN register
        await _append(infra, str(uid), "notify.test", 7)
        message = None
        for _ in range(50):
            message = await sub.get(timeout=0.2)
            if message:
                break
        await loop.stop()
    assert message is not None and message["type"] == "notify.test"
