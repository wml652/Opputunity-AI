from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from app.application.llm.use_cases import ProviderConfigView
from app.domain.identity.models import User
from app.domain.llm.provider import ConnectionCheck, ProviderKind, ProviderPreset


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth -------------------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=10, max_length=256)
    display_name: str = Field(default="", max_length=120)


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: SecretStr = Field(max_length=256)


class UserOut(_Out):
    id: uuid.UUID
    email: str
    display_name: str
    timezone: str

    @staticmethod
    def of(user: User) -> UserOut:
        return UserOut(
            id=user.id, email=user.email, display_name=user.display_name, timezone=user.timezone
        )


class SessionOut(BaseModel):
    user: UserOut
    access_expires_at: datetime
    csrf_token: str


# ---- llm --------------------------------------------------------------------------------------


class PresetOut(_Out):
    """Presets only pre-fill the base URL; models are always user-provided."""

    kind: ProviderKind
    label: str
    base_url: str | None

    @staticmethod
    def of(preset: ProviderPreset) -> PresetOut:
        return PresetOut(
            kind=preset.kind,
            label=preset.label,
            base_url=preset.base_url,
        )


class ProviderConfigCreate(BaseModel):
    kind: ProviderKind
    label: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    api_key: SecretStr = Field(min_length=8, max_length=4096)
    embedding_base_url: str | None = Field(default=None, max_length=2048)
    embedding_model: str | None = Field(default=None, max_length=200)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=4096)
    activate: bool = True


class ProviderConfigUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    embedding_base_url: str | None = Field(default=None, max_length=2048)
    embedding_model: str | None = Field(default=None, max_length=200)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=4096)


class ConnectionCheckOut(_Out):
    ok: bool
    status: Literal["Connection Established", "Connection Failed"]
    latency_ms: int | None
    model: str
    model_listed: bool | None
    error_code: str | None
    error_message: str | None
    checked_at: datetime

    @staticmethod
    def of(check: ConnectionCheck) -> ConnectionCheckOut:
        return ConnectionCheckOut(
            ok=check.ok,
            status="Connection Established" if check.ok else "Connection Failed",
            latency_ms=check.latency_ms,
            model=check.model,
            model_listed=check.model_listed,
            error_code=check.error_code,
            error_message=check.error_message,
            checked_at=check.checked_at,
        )


class ProviderConfigOut(_Out):
    id: uuid.UUID
    kind: ProviderKind
    label: str
    base_url: str
    model: str
    api_key_last4: str
    embedding_base_url: str | None
    embedding_model: str | None
    has_embedding_key: bool
    effective_embedding_model: str
    is_active: bool
    last_check: ConnectionCheckOut | None

    @staticmethod
    def of(view: ProviderConfigView) -> ProviderConfigOut:
        return ProviderConfigOut(
            id=view.id,
            kind=view.kind,
            label=view.label,
            base_url=view.base_url,
            model=view.model,
            api_key_last4=view.api_key_last4,
            embedding_base_url=view.embedding_base_url,
            embedding_model=view.embedding_model,
            has_embedding_key=view.has_embedding_key,
            effective_embedding_model=view.effective_embedding_model,
            is_active=view.is_active,
            last_check=ConnectionCheckOut.of(view.last_check) if view.last_check else None,
        )


class FetchModelsRequest(BaseModel):
    """Either a saved provider (`provider_config_id`) or unsaved form values."""

    provider_config_id: uuid.UUID | None = None
    kind: ProviderKind | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class FetchModelsResponse(BaseModel):
    base_url: str
    models: list[str]


class VerifyRequest(BaseModel):
    provider_config_id: uuid.UUID | None = None


class VerifyResponse(BaseModel):
    provider: ProviderConfigOut
    check: ConnectionCheckOut


class PromptTestRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    provider_config_id: uuid.UUID | None = None


class PromptTestOut(BaseModel):
    provider_config_id: uuid.UUID
    model: str
    text: str
    reasoning: str | None
    finish_reason: str | None
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


class ConnectionStatusOut(BaseModel):
    configured: bool
    provider: ProviderConfigOut | None


# ---- system / events --------------------------------------------------------------------------


class WorkerOut(BaseModel):
    worker_id: str
    queue: str | None = None
    host: str | None = None
    pid: int | None = None
    seen_at: float | None = None


class WorkerStatusOut(BaseModel):
    active: int
    workers: list[WorkerOut]


class EventOut(BaseModel):
    seq: int
    type: str
    payload: dict[str, object]
    campaign_id: str | None
    application_id: str | None
    created_at: str


class InboxSummaryOut(BaseModel):
    open_questions: int
    awaiting_verification: int
    total: int
