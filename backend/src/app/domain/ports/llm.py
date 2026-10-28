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
    def create(self, *, base_url: str, api_key: str, model: str) -> ILLMProvider: ...


class IEmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class IEmbeddingProviderFactory(Protocol):
    def create(self, route: EmbeddingRoute, api_key: str | None) -> IEmbeddingProvider: ...
