from __future__ import annotations

import httpx
import pytest
import respx

from app.domain.llm.provider import ConnectionErrorCode
from app.domain.ports.llm import ChatMessage
from app.infrastructure.llm.http_client import LLMHttpError
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProviderFactory

BASE = "https://openrouter.ai/api/v1"
KEY = "sk-or-v1-unit-test-key-0123456789abcdef"
OK_COMPLETION = {
    "id": "c1",
    "model": "meta-llama/llama-3.3-70b-instruct",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "p"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    f = OpenAICompatibleProviderFactory(timeout=1.0)
    yield f
    await f.aclose()


@respx.mock
async def test_handshake_ok_sends_bearer_and_measures_latency(factory) -> None:  # type: ignore[no-untyped-def]
    chat = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_COMPLETION)
    )
    respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "meta-llama/llama-3.3-70b-instruct"}]}
        )
    )
    provider = factory.create(base_url=BASE, api_key=KEY, model="meta-llama/llama-3.3-70b-instruct")
    result = await provider.handshake()
    assert result.ok and result.model_listed is True
    assert result.latency_ms is not None and result.latency_ms >= 0
    sent = chat.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert b'"max_tokens":1' in sent.content.replace(b" ", b"")


@respx.mock
async def test_handshake_models_probe_optional(factory) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_COMPLETION)
    )
    respx.get(f"{BASE}/models").mock(return_value=httpx.Response(404, json={"error": "nope"}))
    result = await factory.create(base_url=BASE, api_key=KEY, model="m").handshake()
    assert result.ok and result.model_listed is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ConnectionErrorCode.UNAUTHORIZED),
        (403, ConnectionErrorCode.FORBIDDEN),
        (404, ConnectionErrorCode.MODEL_NOT_FOUND),
        (410, ConnectionErrorCode.MODEL_NOT_FOUND),
        (429, ConnectionErrorCode.RATE_LIMITED),
        (503, ConnectionErrorCode.UPSTREAM),
        (400, ConnectionErrorCode.UNKNOWN),
    ],
)
@respx.mock
async def test_handshake_maps_http_errors(factory, status: int, code: ConnectionErrorCode) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            status, json={"error": {"message": f"Incorrect API key provided: {KEY}"}}
        )
    )
    result = await factory.create(base_url=BASE, api_key=KEY, model="m").handshake()
    assert not result.ok and result.error_code is code
    assert result.error_message is not None and KEY not in result.error_message
    assert f"HTTP {status}" in result.error_message


@respx.mock
async def test_handshake_timeout(factory) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    result = await factory.create(base_url=BASE, api_key=KEY, model="m").handshake()
    assert result.error_code is ConnectionErrorCode.TIMEOUT


@respx.mock
async def test_handshake_network_error(factory) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    result = await factory.create(base_url=BASE, api_key=KEY, model="m").handshake()
    assert result.error_code is ConnectionErrorCode.NETWORK


@respx.mock
async def test_complete_retries_rate_limit_then_succeeds(factory) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"}),
            httpx.Response(200, json=OK_COMPLETION),
        ]
    )
    out = await factory.create(base_url=BASE, api_key=KEY, model="m").complete(
        [ChatMessage(role="user", content="hi")], max_tokens=5
    )
    assert out.text == "p" and out.prompt_tokens == 3
    assert route.call_count == 2


@respx.mock
async def test_complete_does_not_retry_auth_errors(factory) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(LLMHttpError) as info:
        await factory.create(base_url=BASE, api_key=KEY, model="m").complete(
            [ChatMessage(role="user", content="hi")]
        )
    assert info.value.code is ConnectionErrorCode.UNAUTHORIZED
    assert route.call_count == 1
