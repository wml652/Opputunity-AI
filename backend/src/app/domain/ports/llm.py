from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.llm.embedding import EmbeddingRoute
from app.domain.llm.provider import HandshakeResult


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning: str | None = None  # reasoning models return their chain separately
    finish_reason: str | None = None


class ILLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion: ...

    async def list_models(self) -> list[str]: ...

    async def handshake(self) -> HandshakeResult:
        """1-token chat round trip (latency measured) plus a `/models` listing probe."""
        ...


class ILLMProviderFactory(Protocol):
    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> ILLMProvider:
        """`rpm_limit`: requests per minute shared by every call with this key (proactive
        pacing, no 429 stalls)."""
        ...


class IRerankProvider(Protocol):
    """A cross-encoder reranker the USER configured (`rerank_model`); never chosen for them."""

    @property
    def model(self) -> str: ...

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance of each passage to `query`, in [0, 1], in input order."""
        ...


class IRerankProviderFactory(Protocol):
    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> IRerankProvider: ...


class IVisionProvider(Protocol):
    """A multimodal model the USER configured (`vision_model`); never chosen for them."""

    @property
    def model(self) -> str: ...

    async def read_image(self, image: bytes, mime: str, instruction: str) -> str:
        """Markdown transcription of the image (tables kept as Markdown tables)."""
        ...


class IVisionProviderFactory(Protocol):
    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> IVisionProvider: ...


class IEmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class IEmbeddingProviderFactory(Protocol):
    def create(self, route: EmbeddingRoute, api_key: str | None) -> IEmbeddingProvider: ...
