"""The embedding space is fixed at 1024 dimensions so pgvector columns never need migrating when
the chat provider changes."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.llm.provider import PRESETS, ProviderKind
from app.domain.shared.errors import DomainError

EMBEDDING_DIM = 1024
LOCAL_HASHING_MODEL = "local/hashing-1024"


class EmbeddingKeySource(enum.StrEnum):
    EMBEDDING = "embedding"  # dedicated embedding key
    CHAT = "chat"  # reuse the chat key (only ever sent to the chat provider's own host)
    NONE = "none"


@dataclass(frozen=True, slots=True)
class EmbeddingRoute:
    local: bool
    base_url: str | None
    model: str
    key_source: EmbeddingKeySource
    nim_input_type: bool  # NVIDIA retrieval models require input_type=query|passage
    request_dimensions: bool  # send `dimensions=EMBEDDING_DIM` (Matryoshka models)


def choose_embedding_route(
    *,
    kind: ProviderKind,
    chat_base_url: str,
    embedding_base_url: str | None,
    embedding_model: str | None,
    has_embedding_key: bool,
) -> EmbeddingRoute:
    """User-provided models only: the exact `embedding_model` the user saved is used, or, when it
    is blank, the deterministic local `HashingEmbedding1024`. No model id is ever assumed.

    The chat key is reused only when embeddings go to the same host as chat, so configuring a
    third-party embedding endpoint can never leak the chat provider's key to it.
    """
    del kind  # the provider kind never implies an embedding model
    model = (embedding_model or "").strip()
    if not model:
        if embedding_base_url:
            raise DomainError("embedding_model is required when an embedding base URL is set")
        return EmbeddingRoute(
            local=True,
            base_url=None,
            model=LOCAL_HASHING_MODEL,
            key_source=EmbeddingKeySource.NONE,
            nim_input_type=False,
            request_dimensions=False,
        )
    base = (embedding_base_url or chat_base_url).rstrip("/")
    if has_embedding_key:
        key_source = EmbeddingKeySource.EMBEDDING
    elif base == chat_base_url.rstrip("/"):
        key_source = EmbeddingKeySource.CHAT
    else:
        key_source = EmbeddingKeySource.NONE
    return EmbeddingRoute(
        local=False,
        base_url=base,
        model=model,
        key_source=key_source,
        # NVIDIA's retrieval endpoints require input_type; decided by host, not by model name.
        nim_input_type=base == PRESETS[ProviderKind.NVIDIA_NIM].base_url,
        # Always request the platform's fixed size; the dimension guard rejects anything else.
        request_dimensions=True,
    )


class EmbeddingDimensionError(DomainError):
    code = "embedding_dimension_mismatch"


def ensure_dim(vectors: Sequence[Sequence[float]], model: str) -> list[list[float]]:
    out: list[list[float]] = []
    for vec in vectors:
        if len(vec) != EMBEDDING_DIM:
            raise EmbeddingDimensionError(
                f"embedding model '{model}' returned {len(vec)} dims; expected {EMBEDDING_DIM}"
            )
        out.append([float(x) for x in vec])
    return out
