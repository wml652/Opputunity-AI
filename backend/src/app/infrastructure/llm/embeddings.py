"""Embedding adapters. All of them emit exactly `EMBEDDING_DIM` (1024) floats or raise."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from typing import Any

import httpx

from app.domain.llm.embedding import EMBEDDING_DIM, EmbeddingRoute, ensure_dim
from app.infrastructure.llm.http_client import OpenAICompatibleClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


class HashingEmbedding1024:
    """Deterministic, dependency-free fallback (signed feature hashing over word unigrams,
    bigrams and character trigrams, L2-normalised). Lower quality than a neural model, but it
    keeps semantic memory usable when no embedding endpoint is configured."""

    model_name = "local/hashing-1024"

    def __init__(self) -> None:
        log.warning(
            "embeddings.degraded_fallback",
            detail="no embedding endpoint configured; using local hashing embeddings",
        )

    @staticmethod
    def _features(text: str) -> list[tuple[str, float]]:
        words = _TOKEN_RE.findall(text.lower())
        feats: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        feats += [(f"b:{a}_{b}", 0.7) for a, b in itertools.pairwise(words)]
        for w in words:
            padded = f"#{w}#"
            feats += [(f"c:{padded[i : i + 3]}", 0.3) for i in range(len(padded) - 2)]
        return feats

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * EMBEDDING_DIM
        for feat, weight in self._features(text):
            digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % EMBEDDING_DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return ensure_dim([self._embed(t) for t in texts], self.model_name)

    async def embed_query(self, text: str) -> list[float]:
        return ensure_dim([self._embed(text)], self.model_name)[0]


class OpenAICompatibleEmbedding:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        nim_input_type: bool,
        request_dimensions: bool,
        http_client: httpx.AsyncClient,
        timeout: float,
    ) -> None:
        self.model_name = model
        self._nim = nim_input_type
        self._request_dimensions = request_dimensions
        self._client = OpenAICompatibleClient(
            base_url=base_url, api_key=api_key, http_client=http_client, timeout=timeout
        )

    async def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "input": list(texts),
            "encoding_format": "float",
        }
        if self._nim:
            body |= {"input_type": input_type, "truncate": "END"}
        if self._request_dimensions:
            body["dimensions"] = EMBEDDING_DIM
        response = await self._client.request("POST", "/embeddings", json=body, max_retries=2)
        data = sorted(response.get("data", []), key=lambda d: int(d.get("index", 0)))
        return ensure_dim([d["embedding"] for d in data], self.model_name)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed(texts, "passage")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]


class EmbeddingProviderFactory:
    def __init__(self, *, http_client: httpx.AsyncClient, timeout: float) -> None:
        self._http = http_client
        self._timeout = timeout

    def create(
        self, route: EmbeddingRoute, api_key: str | None
    ) -> HashingEmbedding1024 | OpenAICompatibleEmbedding:
        if route.local or route.base_url is None:
            return HashingEmbedding1024()
        return OpenAICompatibleEmbedding(
            base_url=route.base_url,
            api_key=api_key,
            model=route.model,
            nim_input_type=route.nim_input_type,
            request_dimensions=route.request_dimensions,
            http_client=self._http,
            timeout=self._timeout,
        )
