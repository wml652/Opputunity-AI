from __future__ import annotations

import json
import logging

import asyncpg
import httpx
import pytest
import respx

from app.bootstrap.container import Infrastructure
from app.infrastructure.logging import configure_logging
from tests.conftest import ApiUser, DatabaseUrls

NIM = "https://integrate.api.nvidia.com/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
NIM_KEY = "nvapi-REAL-LOOKING-key-AbCdEf0123456789xyz"
OR_KEY = "sk-or-v1-REAL-LOOKING-key-0123456789abcdef"
COMPLETION = {
    "id": "x",
    "model": "nvidia/nemotron-3-super-120b-a12b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "p"}}],
}


def _models(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


async def _create(user: ApiUser, **body: object) -> dict[str, object]:
    resp = await user.post("/api/v1/llm/providers", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, object] = resp.json()
    return data


async def test_presets_listed(user: ApiUser) -> None:
    presets = {p["kind"]: p for p in (await user.get("/api/v1/llm/presets")).json()}
    assert presets["NVIDIA_NIM"]["base_url"] == NIM
    assert presets["OPENROUTER"]["base_url"] == OPENROUTER
    # Presets carry only the base URL: no model ids are ever assumed.
    assert all(set(p) == {"kind", "label", "base_url"} for p in presets.values())
    assert presets["CUSTOM"]["base_url"] is None


async def test_create_pins_preset_url_and_never_returns_key(
    user: ApiUser, clean_db: DatabaseUrls
) -> None:
    data = await _create(
        user,
        kind="NVIDIA_NIM",
        model="test/chat-model",
        api_key=NIM_KEY,
        base_url="https://evil.example.com/v1",
    )
    assert data["base_url"] == NIM  # client-supplied URL ignored for presets
    assert data["api_key_last4"] == NIM_KEY[-4:]
    assert data["model"] == "test/chat-model"  # exactly what the user typed
    assert data["effective_embedding_model"] == "local/hashing-1024"  # blank -> local, no guess
    assert data["is_active"] is True
    listing = await user.get("/api/v1/llm/providers")
    for text in (str(data), listing.text):
        assert NIM_KEY not in text and NIM_KEY[:-4] not in text

    conn = await asyncpg.connect(clean_db.owner)
    try:
        row = await conn.fetchrow(
            "SELECT api_key_ciphertext, key_version FROM llm_provider_configs"
        )
    finally:
        await conn.close()
    assert row is not None and row["key_version"] == 1
    assert NIM_KEY.encode() not in bytes(row["api_key_ciphertext"])


async def test_openrouter_falls_back_to_local_embeddings(user: ApiUser) -> None:
    data = await _create(user, kind="OPENROUTER", model="test/chat-model", api_key=OR_KEY)
    assert data["base_url"] == OPENROUTER
    assert data["effective_embedding_model"] == "local/hashing-1024"


async def test_custom_provider_ssrf_guard(user: ApiUser) -> None:
    bad = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "CUSTOM",
            "api_key": OR_KEY,
            "model": "m",
            "base_url": "https://gateway.internal-test/v1",
        },
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unsafe_url"
    meta = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "CUSTOM",
            "api_key": OR_KEY,
            "model": "m",
            "base_url": "http://169.254.169.254/latest",
        },
    )
    assert meta.status_code == 422
    ok = await _create(
        user, kind="CUSTOM", api_key=OR_KEY, model="m", base_url="https://llm.example.com/v1/"
    )
    assert ok["base_url"] == "https://llm.example.com/v1"


async def test_activation_is_exclusive(user: ApiUser) -> None:
    first = await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)
    second = await _create(user, kind="OPENROUTER", model="test/chat-model", api_key=OR_KEY)
    configs = {c["id"]: c for c in (await user.get("/api/v1/llm/providers")).json()}
    assert configs[second["id"]]["is_active"] and not configs[first["id"]]["is_active"]
    await user.post(f"/api/v1/llm/providers/{first['id']}/activate")
    status = (await user.get("/api/v1/llm/status")).json()
    assert status["configured"] and status["provider"]["id"] == first["id"]


async def test_patch_rotates_key_and_embedding_override(user: ApiUser) -> None:
    created = await _create(user, kind="OPENROUTER", model="test/chat-model", api_key=OR_KEY)
    patched = await user.patch(
        f"/api/v1/llm/providers/{created['id']}",
        json={
            "api_key": "sk-or-v1-NEWKEY-9999888877776666",
            "embedding_base_url": NIM,
            "embedding_model": "user/embedder-1024",
            "embedding_api_key": "nvapi-embedding-only-key-123456",
        },
    )
    body = patched.json()
    assert patched.status_code == 200, body
    assert body["api_key_last4"] == "6666"
    assert body["has_embedding_key"] and body["effective_embedding_model"] == "user/embedder-1024"
    cleared = await user.patch(
        f"/api/v1/llm/providers/{created['id']}",
        json={"embedding_base_url": None, "embedding_api_key": None, "embedding_model": None},
    )
    assert cleared.json()["effective_embedding_model"] == "local/hashing-1024"


@respx.mock
async def test_verify_nim_success_persists_and_emits_event(
    user: ApiUser, clean_db: DatabaseUrls
) -> None:
    chat = respx.post(f"{NIM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    respx.get(f"{NIM}/models").mock(return_value=_models("test/chat-model"))
    await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)

    resp = await user.post("/api/v1/llm/verify", json={})
    assert resp.status_code == 200, resp.text
    check = resp.json()["check"]
    assert check["ok"] is True and check["status"] == "Connection Established"
    assert isinstance(check["latency_ms"], int) and check["latency_ms"] >= 0
    assert check["model_listed"] is True
    assert chat.calls.last.request.headers["authorization"] == f"Bearer {NIM_KEY}"
    assert NIM_KEY not in resp.text

    status = (await user.get("/api/v1/llm/status")).json()
    assert status["provider"]["last_check"]["status"] == "Connection Established"

    events = (await user.get("/api/v1/events")).json()
    llm_events = [e for e in events if e["type"] == "llm.status"]
    assert llm_events and llm_events[-1]["payload"]["ok"] is True
    assert llm_events[-1]["payload"]["latency_ms"] == check["latency_ms"]

    conn = await asyncpg.connect(clean_db.owner)
    try:
        persisted = await conn.fetchval("SELECT count(*) FROM llm_connection_checks WHERE ok")
    finally:
        await conn.close()
    assert persisted == 1


@respx.mock
async def test_verify_openrouter_unauthorized_is_redacted(user: ApiUser) -> None:
    respx.post(f"{OPENROUTER}/chat/completions").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": f"Invalid API key: {OR_KEY}", "code": 401}}
        )
    )
    await _create(user, kind="OPENROUTER", model="test/chat-model", api_key=OR_KEY)
    resp = await user.post("/api/v1/llm/verify")
    check = resp.json()["check"]
    assert resp.status_code == 200
    assert check["ok"] is False and check["status"] == "Connection Failed"
    assert check["error_code"] == "unauthorized" and check["latency_ms"] is None
    assert OR_KEY not in resp.text
    events = (await user.get("/api/v1/events")).json()
    assert OR_KEY not in str(events)
    assert events[-1]["payload"]["ok"] is False


@respx.mock
async def test_verify_timeout(user: ApiUser) -> None:
    respx.post(f"{NIM}/chat/completions").mock(side_effect=httpx.ReadTimeout("upstream slow"))
    await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)
    check = (await user.post("/api/v1/llm/verify")).json()["check"]
    assert check["ok"] is False and check["error_code"] == "timeout"


async def test_verify_without_provider_is_404(user: ApiUser) -> None:
    assert (await user.post("/api/v1/llm/verify")).status_code == 404


@respx.mock
async def test_keys_never_reach_logs(
    user: ApiUser, caplog: pytest.LogCaptureFixture, infra: Infrastructure
) -> None:
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    respx.post(f"{NIM}/chat/completions").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"message": f"bad key {NIM_KEY}"}}),
            httpx.Response(200, json=COMPLETION),
        ]
    )
    respx.get(f"{NIM}/models").mock(
        return_value=httpx.Response(500, text=f"echo Authorization: Bearer {NIM_KEY}")
    )
    await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)
    await user.post("/api/v1/llm/verify")
    await user.post("/api/v1/llm/verify")
    logging.getLogger("httpx").info("request headers: Authorization: Bearer %s", NIM_KEY)

    rendered = caplog.text + " ".join(str(r.msg) for r in caplog.records)
    assert caplog.records, "expected log output"
    assert "llm.handshake_failed" in rendered and "llm.handshake_ok" in rendered
    assert NIM_KEY not in rendered
    assert NIM_KEY[6:-4] not in rendered


@respx.mock
async def test_prompt_test_returns_generation(user: ApiUser) -> None:
    route = respx.post(f"{NIM}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Paris.",
                            "reasoning_content": "The capital of France is Paris.",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )
    )
    await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)
    resp = await user.post(
        "/api/v1/llm/test-prompt", json={"prompt": "Capital of France?", "max_tokens": 64}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "Paris."
    assert body["reasoning"] == "The capital of France is Paris."
    assert body["finish_reason"] == "stop"
    assert body["completion_tokens"] == 3 and body["latency_ms"] >= 0
    sent = route.calls.last.request.content.replace(b" ", b"")
    assert b"CapitalofFrance?" in sent and b'"max_tokens":64' in sent


@respx.mock
async def test_prompt_test_upstream_error_is_502_and_redacted(user: ApiUser) -> None:
    respx.post(f"{NIM}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": f"bad key {NIM_KEY}"}})
    )
    await _create(user, kind="NVIDIA_NIM", model="test/chat-model", api_key=NIM_KEY)
    resp = await user.post("/api/v1/llm/test-prompt", json={"prompt": "hi"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "unauthorized"
    assert NIM_KEY not in resp.text


async def test_prompt_test_without_provider_is_404(user: ApiUser) -> None:
    assert (await user.post("/api/v1/llm/test-prompt", json={"prompt": "hi"})).status_code == 404


async def test_model_is_required_and_never_substituted(user: ApiUser) -> None:
    missing = await user.post(
        "/api/v1/llm/providers", json={"kind": "NVIDIA_NIM", "api_key": NIM_KEY}
    )
    assert missing.status_code == 422
    assert "model is required" in missing.json()["error"]["message"]
    created = await _create(user, kind="NVIDIA_NIM", model="my/exact-model-id", api_key=NIM_KEY)
    assert created["model"] == "my/exact-model-id"
    assert created["effective_embedding_model"] == "local/hashing-1024"
    with_emb = await _create(
        user, kind="NVIDIA_NIM", model="my/chat", api_key=NIM_KEY, embedding_model="my/embedder"
    )
    assert with_emb["effective_embedding_model"] == "my/embedder"


@respx.mock
async def test_verify_uses_exact_saved_model(user: ApiUser) -> None:
    route = respx.post(f"{NIM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    respx.get(f"{NIM}/models").mock(return_value=_models("vendor/custom-model-x"))
    await _create(user, kind="NVIDIA_NIM", model="vendor/custom-model-x", api_key=NIM_KEY)
    await user.post("/api/v1/llm/verify")
    assert json.loads(route.calls.last.request.content)["model"] == "vendor/custom-model-x"


@respx.mock
async def test_fetch_models_with_unsaved_key_and_saved_config(user: ApiUser) -> None:
    listing = respx.get(f"{NIM}/models").mock(return_value=_models("z/model", "a/model", "a/model"))
    resp = await user.post(
        "/api/v1/llm/fetch-models", json={"kind": "NVIDIA_NIM", "api_key": NIM_KEY}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"base_url": NIM, "models": ["a/model", "z/model"]}
    assert listing.calls.last.request.headers["authorization"] == f"Bearer {NIM_KEY}"
    assert NIM_KEY not in resp.text
    saved = await _create(user, kind="NVIDIA_NIM", model="a/model", api_key=NIM_KEY)
    again = await user.post("/api/v1/llm/fetch-models", json={"provider_config_id": saved["id"]})
    assert again.json()["models"] == ["a/model", "z/model"]
    bad = await user.post(
        "/api/v1/llm/fetch-models",
        json={"kind": "CUSTOM", "api_key": OR_KEY, "base_url": "https://x.internal-test/v1"},
    )
    assert bad.status_code == 422


@respx.mock
async def test_fetch_models_upstream_auth_error(user: ApiUser) -> None:
    respx.get(f"{NIM}/models").mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    resp = await user.post(
        "/api/v1/llm/fetch-models", json={"kind": "NVIDIA_NIM", "api_key": NIM_KEY}
    )
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "unauthorized"
