"""Optional user-configured rerank and vision models (OpenAI-compatible / NVIDIA NIM style).

Neither model is ever chosen for the user: they exist only when the user typed `rerank_model` /
`vision_model` in LLM Gateway. Without them discovery uses the local hybrid scorer and pypdf.

* Reranker: `POST {base}/ranking` with `{"model", "query": {"text"}, "passages": [{"text"}]}` and
  `{"rankings": [{"index", "logit"}]}` back (NIM retrieval API; self-hosted NIMs expose
  `/v1/ranking`). Logits become [0, 1] relevance via a sigmoid.
* Vision: `POST {base}/chat/completions` with an `image_url` data-URI content part."""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from typing import Any

from app.domain.llm.provider import ConnectionErrorCode
from app.infrastructure.llm.http_client import LLMHttpError, OpenAICompatibleClient

MAX_PASSAGES = 64
MAX_PASSAGE_CHARS = 2_000
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, x))))


class OpenAICompatibleReranker:
    def __init__(self, client: OpenAICompatibleClient, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        scores = [0.0] * len(passages)
        for start in range(0, len(passages), MAX_PASSAGES):
            batch = passages[start : start + MAX_PASSAGES]
            body = await self._client.request(
                "POST",
                "/ranking",
                json={
                    "model": self._model,
                    "query": {"text": query[:2_000]},
                    "passages": [{"text": p[:MAX_PASSAGE_CHARS]} for p in batch],
                    "truncate": "END",
                },
                max_retries=1,
            )
            rankings = body.get("rankings")
            if not isinstance(rankings, list):
                raise LLMHttpError(ConnectionErrorCode.UPSTREAM, "reranker returned no rankings")
            for item in rankings:
                if not isinstance(item, dict):
                    continue
                index = item.get("index")
                if isinstance(index, int) and 0 <= index < len(batch):
                    raw: Any = item.get("logit", item.get("score", 0.0))
                    value = float(raw) if isinstance(raw, int | float) else 0.0
                    # Logits are unbounded; probabilities already are in [0, 1].
                    scores[start + index] = (
                        value if 0.0 <= value <= 1.0 and "score" in item else (_sigmoid(value))
                    )
        return scores


class OpenAICompatibleVision:
    def __init__(self, client: OpenAICompatibleClient, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def read_image(self, image: bytes, mime: str, instruction: str) -> str:
        if len(image) > MAX_IMAGE_BYTES:
            raise LLMHttpError(ConnectionErrorCode.UPSTREAM, "image too large for the vision model")
        data_uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body = await self._client.request(
            "POST",
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
                "max_tokens": 4_096,
                "temperature": 0.0,
            },
            max_retries=1,
        )
        choices = body.get("choices") or []
        message = (choices[0] if choices else {}).get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else ""
