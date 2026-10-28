from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.shared.errors import DomainError


class ProviderKind(enum.StrEnum):
    NVIDIA_NIM = "NVIDIA_NIM"
    OPENROUTER = "OPENROUTER"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    kind: ProviderKind
    label: str
    # Presets ONLY pre-fill the base URL (None = user supplies it, CUSTOM). They carry no model
    # ids: models are always typed or picked by the user (CLAUDE.md: user-provided models only).
    base_url: str | None


PRESETS: dict[ProviderKind, ProviderPreset] = {
    ProviderKind.NVIDIA_NIM: ProviderPreset(
        kind=ProviderKind.NVIDIA_NIM,
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
    ),
    ProviderKind.OPENROUTER: ProviderPreset(
        kind=ProviderKind.OPENROUTER,
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    ),
    ProviderKind.CUSTOM: ProviderPreset(
        kind=ProviderKind.CUSTOM,
        label="Custom (OpenAI-compatible)",
        base_url=None,
    ),
}


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: int


@dataclass(slots=True)
class ProviderConfig:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: ProviderKind
    label: str
    base_url: str
    model: str
    api_key: EncryptedSecret
    api_key_last4: str
    embedding_base_url: str | None
    embedding_model: str | None
    embedding_api_key: EncryptedSecret | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ConnectionErrorCode(enum.StrEnum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UPSTREAM = "upstream_error"
    UNKNOWN = "unknown"


class LLMProviderError(DomainError):
    """The upstream LLM provider rejected or failed a request. `code` is a ConnectionErrorCode
    value; `message` is already redacted by the adapter."""

    code = "llm_provider_error"


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    ok: bool
    latency_ms: int | None
    model: str
    model_listed: bool | None
    error_code: ConnectionErrorCode | None
    error_message: str | None


@dataclass(slots=True)
class ConnectionCheck:
    id: uuid.UUID
    user_id: uuid.UUID
    provider_config_id: uuid.UUID
    ok: bool
    latency_ms: int | None
    model: str
    model_listed: bool | None
    error_code: str | None
    error_message: str | None
    checked_at: datetime


def last4(secret: str) -> str:
    return secret[-4:] if len(secret) >= 8 else "****"
