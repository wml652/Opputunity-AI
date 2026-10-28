from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import Container, CurrentUserId
from app.api.v1.schemas import (
    ConnectionCheckOut,
    ConnectionStatusOut,
    FetchModelsRequest,
    FetchModelsResponse,
    PresetOut,
    PromptTestOut,
    PromptTestRequest,
    ProviderConfigCreate,
    ProviderConfigOut,
    ProviderConfigUpdate,
    VerifyRequest,
    VerifyResponse,
)
from app.application.llm.use_cases import ProviderConfigInput, ProviderConfigPatch
from app.domain.llm.provider import PRESETS

router = APIRouter(prefix="/llm", tags=["llm"])


def _secret(value: object) -> str | None:
    get = getattr(value, "get_secret_value", None)
    return get() if callable(get) else None


@router.get("/presets", response_model=list[PresetOut])
async def presets(_user: CurrentUserId) -> list[PresetOut]:
    return [PresetOut.of(p) for p in PRESETS.values()]


@router.get("/providers", response_model=list[ProviderConfigOut])
async def list_providers(user_id: CurrentUserId, container: Container) -> list[ProviderConfigOut]:
    return [ProviderConfigOut.of(v) for v in await container.list_providers.execute(user_id)]


@router.post("/providers", response_model=ProviderConfigOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: ProviderConfigCreate, user_id: CurrentUserId, container: Container
) -> ProviderConfigOut:
    view = await container.create_provider.execute(
        user_id,
        ProviderConfigInput(
            kind=body.kind,
            label=body.label,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key.get_secret_value(),
            embedding_base_url=body.embedding_base_url,
            embedding_model=body.embedding_model,
            embedding_api_key=_secret(body.embedding_api_key),
            activate=body.activate,
        ),
    )
    return ProviderConfigOut.of(view)


@router.patch("/providers/{config_id}", response_model=ProviderConfigOut)
async def update_provider(
    config_id: uuid.UUID, body: ProviderConfigUpdate, user_id: CurrentUserId, container: Container
) -> ProviderConfigOut:
    view = await container.update_provider.execute(
        user_id,
        config_id,
        ProviderConfigPatch(
            label=body.label,
            base_url=body.base_url,
            model=body.model,
            api_key=_secret(body.api_key),
            embedding_base_url=body.embedding_base_url,
            embedding_model=body.embedding_model,
            embedding_api_key=_secret(body.embedding_api_key),
            fields_set=frozenset(body.model_fields_set),
        ),
    )
    return ProviderConfigOut.of(view)


@router.post("/providers/{config_id}/activate", response_model=ProviderConfigOut)
async def activate_provider(
    config_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> ProviderConfigOut:
    return ProviderConfigOut.of(await container.activate_provider.execute(user_id, config_id))


@router.delete("/providers/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    config_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    await container.delete_provider.execute(user_id, config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/fetch-models", response_model=FetchModelsResponse)
async def fetch_models(
    body: FetchModelsRequest, user_id: CurrentUserId, container: Container
) -> FetchModelsResponse:
    base_url, models = await container.fetch_models.execute(
        user_id,
        config_id=body.provider_config_id,
        kind=body.kind,
        base_url=body.base_url,
        api_key=_secret(body.api_key),
    )
    return FetchModelsResponse(base_url=base_url, models=models)


@router.post("/verify", response_model=VerifyResponse)
async def verify(
    user_id: CurrentUserId, container: Container, body: VerifyRequest | None = None
) -> VerifyResponse:
    view, check = await container.verify_connection.execute(
        user_id, body.provider_config_id if body else None
    )
    return VerifyResponse(provider=ProviderConfigOut.of(view), check=ConnectionCheckOut.of(check))


@router.post("/test-prompt", response_model=PromptTestOut)
async def test_prompt(
    body: PromptTestRequest, user_id: CurrentUserId, container: Container
) -> PromptTestOut:
    result = await container.test_prompt.execute(
        user_id, body.prompt, max_tokens=body.max_tokens, config_id=body.provider_config_id
    )
    return PromptTestOut(
        provider_config_id=result.provider_config_id,
        model=result.model,
        text=result.text,
        reasoning=result.reasoning,
        finish_reason=result.finish_reason,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


@router.get("/status", response_model=ConnectionStatusOut)
async def connection_status(user_id: CurrentUserId, container: Container) -> ConnectionStatusOut:
    view = await container.connection_status.execute(user_id)
    return ConnectionStatusOut(
        configured=view is not None, provider=ProviderConfigOut.of(view) if view else None
    )
