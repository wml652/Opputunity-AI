from __future__ import annotations

import json
import math

import httpx
import pytest
import respx

from app.domain.llm.embedding import (
    EMBEDDING_DIM,
    LOCAL_HASHING_MODEL,
    EmbeddingDimensionError,
    EmbeddingKeySource,
    choose_embedding_route,
    ensure_dim,
)
from app.domain.llm.provider import ProviderKind
from app.domain.shared.errors import DomainError
from app.infrastructure.llm.embeddings import EmbeddingProviderFactory, HashingEmbedding1024

NIM = "https://integrate.api.nvidia.com/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


def route(**kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "kind": ProviderKind.CUSTOM,
        "chat_base_url": OPENROUTER,
        "embedding_base_url": None,
        "embedding_model": None,
        "has_embedding_key": False,
    }
    args.update(kw)
    return choose_embedding_route(**args)  # type: ignore[arg-type]


def test_no_embedding_model_means_local_even_for_nim() -> None:
    """User-provided models only: a NIM chat config never implies an embedding model."""
    r = route(kind=ProviderKind.NVIDIA_NIM, chat_base_url=NIM)
    assert r.local and r.model == LOCAL_HASHING_MODEL
    r = route(kind=ProviderKind.OPENROUTER, chat_base_url=OPENROUTER)
    assert r.local and r.model == LOCAL_HASHING_MODEL


def test_exact_user_model_on_chat_host_reuses_chat_key() -> None:
    r = route(kind=ProviderKind.NVIDIA_NIM, chat_base_url=NIM, embedding_model=" my/embedder ")
    assert (r.local, r.model, r.base_url) == (False, "my/embedder", NIM)
    assert r.key_source is EmbeddingKeySource.CHAT
    assert r.nim_input_type and r.request_dimensions


def test_override_on_other_host_never_reuses_chat_key() -> None:
    r = route(
        kind=ProviderKind.OPENROUTER, embedding_base_url=NIM, embedding_model="their/embedder"
    )
    assert r.base_url == NIM and r.model == "their/embedder"
    assert r.key_source is EmbeddingKeySource.NONE
    r2 = route(
        kind=ProviderKind.OPENROUTER,
        embedding_base_url=NIM,
        embedding_model="their/embedder",
        has_embedding_key=True,
    )
    assert r2.key_source is EmbeddingKeySource.EMBEDDING


def test_non_nvidia_host_gets_no_input_type() -> None:
    r = route(embedding_model="some/embedder-1024")
    assert r.base_url == OPENROUTER and r.key_source is EmbeddingKeySource.CHAT
    assert not r.nim_input_type
    assert r.request_dimensions


def test_embedding_base_url_requires_model() -> None:
    with pytest.raises(DomainError):
        route(embedding_base_url="https://emb.example.com/v1")


def test_ensure_dim_guard() -> None:
    assert len(ensure_dim([[0.0] * EMBEDDING_DIM], "m")[0]) == EMBEDDING_DIM
    with pytest.raises(EmbeddingDimensionError):
        ensure_dim([[0.0] * 768], "m")


async def test_hashing_embedding_is_deterministic_normalised_and_semantic() -> None:
    emb = HashingEmbedding1024()
    a = await emb.embed_query("What is your expected graduation year?")
    b = await emb.embed_query("What is your expected graduation year?")
    c = await emb.embed_query("When do you expect to graduate (year)?")
    d = await emb.embed_query("Describe a time you led a robotics team")
    assert len(a) == EMBEDDING_DIM and a == b
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-6)

    def cos(x: list[float], y: list[float]) -> float:
        return sum(i * j for i, j in zip(x, y, strict=True))

    assert cos(a, c) > cos(a, d)
    docs = await emb.embed_documents(["one", "two"])
    assert len(docs) == 2 and all(len(v) == EMBEDDING_DIM for v in docs)


@respx.mock
async def test_nim_embedding_sends_input_type_and_validates_dim() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        n = len(body["input"])
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [0.01] * EMBEDDING_DIM} for i in range(n)]},
        )

    respx.post(f"{NIM}/embeddings").mock(side_effect=handler)
    async with httpx.AsyncClient() as http:
        factory = EmbeddingProviderFactory(http_client=http, timeout=2)
        provider = factory.create(
            route(kind=ProviderKind.NVIDIA_NIM, chat_base_url=NIM, embedding_model="user/embedder"),
            "nvapi-test-key-123456",
        )
        assert provider.model_name == "user/embedder"
        q = await provider.embed_query("hello")
        docs = await provider.embed_documents(["a", "b"])
    assert len(q) == EMBEDDING_DIM and len(docs) == 2
    assert calls[0]["input_type"] == "query" and calls[1]["input_type"] == "passage"
    assert calls[0]["model"] == "user/embedder"
    assert calls[0]["dimensions"] == EMBEDDING_DIM  # Matryoshka model pinned to 1024


@respx.mock
async def test_wrong_dimension_from_provider_raises() -> None:
    respx.post(f"{NIM}/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]})
    )
    async with httpx.AsyncClient() as http:
        provider = EmbeddingProviderFactory(http_client=http, timeout=2).create(
            route(kind=ProviderKind.NVIDIA_NIM, chat_base_url=NIM, embedding_model="user/embedder"),
            "nvapi-test-key-123456",
        )
        with pytest.raises(EmbeddingDimensionError):
            await provider.embed_query("hello")


def test_factory_returns_local_fallback() -> None:
    http = httpx.AsyncClient()
    provider = EmbeddingProviderFactory(http_client=http, timeout=2).create(
        route(kind=ProviderKind.OPENROUTER), None
    )
    assert isinstance(provider, HashingEmbedding1024)
