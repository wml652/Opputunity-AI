from __future__ import annotations

from typing import Any

from app.bootstrap.container import Infrastructure, build_relay
from app.infrastructure.workers.tasks import heartbeat, make_worker_id, ping, relay_outbox
from tests.conftest import ApiUser


async def test_worker_heartbeat_visible_in_telemetry(user: ApiUser, infra: Infrastructure) -> None:
    assert (await user.get("/api/v1/system/workers")).json()["active"] == 0
    ctx: dict[str, Any] = {
        "registry": infra.worker_registry,
        "worker_id": make_worker_id("system"),
        "queue_name": "system",
        "heartbeat_ttl": 30,
    }
    await heartbeat(ctx)
    status = (await user.get("/api/v1/system/workers")).json()
    assert status["active"] == 1
    assert status["workers"][0]["queue"] == "system"
    assert await ping(ctx) == "pong"


async def test_relay_task_drains_and_marks_published(user: ApiUser, infra: Infrastructure) -> None:
    await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": "test/chat-model",
            "api_key": "nvapi-test-key-000000000000",
        },
    )
    drained = await relay_outbox({"relay": build_relay(infra)})
    assert drained >= 1
    assert await relay_outbox({"relay": build_relay(infra)}) == 0


async def test_inbox_summary_placeholder(user: ApiUser) -> None:
    assert (await user.get("/api/v1/inbox/summary")).json() == {
        "open_questions": 0,
        "awaiting_verification": 0,
        "total": 0,
    }
