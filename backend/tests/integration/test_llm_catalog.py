"""The full model catalog for the user's key, categorised for the picker, and switching the
active provider's models in place (no delete / re-create)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.bootstrap.app import create_app_with
from app.bootstrap.container import Infrastructure
from tests.conftest import register
from tests.fakes import FakeLLM, FakeLLMFactory

LISTED = [
    "acme/chat-large-70b-instruct",
    "acme/tiny-8b-instruct",
    "acme/reasoner-r1",
    "acme/vision-90b-instruct",
    "acme/nv-embedqa-e5-v5",
    "acme/model-340b-reward",
]


@pytest.fixture
async def cclient(
    infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    real = infra.llm_factory
    fake = FakeLLMFactory()
    infra.llm_factory = fake  # type: ignore[assignment]

    async def list_models(self: FakeLLM) -> list[str]:
        return list(LISTED)

    monkeypatch.setattr(FakeLLM, "list_models", list_models)
    app = create_app_with(infra, run_relay=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    infra.llm_factory = real
    await fake.aclose()


async def test_catalog_is_complete_and_categorised_and_models_switch_in_place(
    cclient: httpx.AsyncClient,
) -> None:
    user = await register(cclient, "catalog@example.com")
    created = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": "acme/chat-large-70b-instruct",
            "api_key": "nvapi-test-key-000000000000",
        },
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    fetched = await user.post("/api/v1/llm/fetch-models", json={"provider_config_id": pid})
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert sorted(body["models"]) == sorted(LISTED)  # everything the key can see
    cats = {m["id"]: (m["category"], m["chat_capable"]) for m in body["catalog"]}
    assert cats["acme/reasoner-r1"] == ("reasoning", True)
    assert cats["acme/nv-embedqa-e5-v5"] == ("embedding", False)
    assert cats["acme/model-340b-reward"] == ("other", False)
    assert body["counts"]["chat"] == 1 and body["counts"]["fast"] == 1

    switched = await user.client.patch(
        f"/api/v1/llm/providers/{pid}",
        json={"model": "acme/reasoner-r1", "extraction_model": "acme/tiny-8b-instruct"},
        headers={"x-csrf-token": user.csrf},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["model"] == "acme/reasoner-r1"
    assert switched.json()["extraction_model"] == "acme/tiny-8b-instruct"
    (provider,) = (await user.get("/api/v1/llm/providers")).json()
    assert provider["id"] == pid and provider["model"] == "acme/reasoner-r1"  # same row
    verify = await user.post("/api/v1/llm/verify", json={"provider_config_id": pid})
    assert verify.status_code == 200 and verify.json()["check"]["ok"] is True
