from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from app.domain.events.models import EventType, NewEvent
from app.domain.llm.embedding import EmbeddingKeySource, choose_embedding_route
from app.domain.llm.provider import (
    PRESETS,
    ConnectionCheck,
    EncryptedSecret,
    ProviderConfig,
    ProviderKind,
    last4,
)
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.llm import (
    ChatMessage,
    IEmbeddingProvider,
    IEmbeddingProviderFactory,
    ILLMProvider,
    ILLMProviderFactory,
)
from app.domain.ports.net import IUrlGuard
from app.domain.ports.uow import IUnitOfWork, IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor
from app.domain.shared.errors import NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

TABLE = "llm_provider_configs"


@dataclass(frozen=True, slots=True)
class ProviderConfigView:
    """Safe projection: never contains key material."""

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
    last_check: ConnectionCheck | None

    @staticmethod
    def of(config: ProviderConfig, last_check: ConnectionCheck | None) -> ProviderConfigView:
        route = choose_embedding_route(
            kind=config.kind,
            chat_base_url=config.base_url,
            embedding_base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            has_embedding_key=config.embedding_api_key is not None,
        )
        return ProviderConfigView(
            id=config.id,
            kind=config.kind,
            label=config.label,
            base_url=config.base_url,
            model=config.model,
            api_key_last4=config.api_key_last4,
            embedding_base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            has_embedding_key=config.embedding_api_key is not None,
            effective_embedding_model=route.model,
            is_active=config.is_active,
            last_check=last_check,
        )


@dataclass(frozen=True, slots=True)
class LLMDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    url_guard: IUrlGuard
    provider_factory: ILLMProviderFactory
    embedding_factory: IEmbeddingProviderFactory
    clock: Clock


@dataclass(frozen=True, slots=True)
class ProviderConfigInput:
    kind: ProviderKind
    label: str | None
    base_url: str | None
    model: str | None
    api_key: str
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    activate: bool = True


@dataclass(frozen=True, slots=True)
class ProviderConfigPatch:
    """Partial update. Embedding fields are applied only when named in `fields_set`, so an
    explicit null clears them while an omitted field leaves them untouched."""

    label: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    fields_set: frozenset[str] = field(default_factory=frozenset)


async def _resolve_base_url(deps: LLMDeps, kind: ProviderKind, base_url: str | None) -> str:
    preset = PRESETS[kind]
    if preset.base_url is not None:
        return preset.base_url  # presets are pinned; never trust a client-supplied URL for them
    if not base_url:
        raise ValidationError("base_url is required for custom providers")
    return (await deps.url_guard.validate(base_url)).rstrip("/")


def _encrypt(
    deps: LLMDeps, column: str, user_id: uuid.UUID, row_id: uuid.UUID, secret: str
) -> EncryptedSecret:
    return deps.cipher.encrypt(secret.encode(), aad(TABLE, column, user_id, row_id))


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


async def _emit_config_changed(uow: IUnitOfWork, user_id: uuid.UUID, config_id: uuid.UUID) -> None:
    await uow.events.append(
        NewEvent(
            user_id=user_id,
            type=EventType.LLM_CONFIG_CHANGED,
            payload={"provider_config_id": str(config_id)},
        )
    )


@dataclass(slots=True)
class CreateProviderConfig:
    deps: LLMDeps

    async def execute(self, user_id: uuid.UUID, data: ProviderConfigInput) -> ProviderConfigView:
        api_key = data.api_key.strip()
        if len(api_key) < 8:
            raise ValidationError("api_key looks invalid")
        preset = PRESETS[data.kind]
        base_url = await _resolve_base_url(self.deps, data.kind, data.base_url)
        model = _clean(data.model)
        if not model:
            raise ValidationError(
                "model is required: enter the exact model id or load it from the API"
            )
        embedding_base_url = _clean(data.embedding_base_url)
        if embedding_base_url:
            embedding_base_url = (await self.deps.url_guard.validate(embedding_base_url)).rstrip(
                "/"
            )
        now = self.deps.clock.now()
        row_id = new_uuid7()
        embedding_key = _clean(data.embedding_api_key)
        config = ProviderConfig(
            id=row_id,
            user_id=user_id,
            kind=data.kind,
            label=_clean(data.label) or preset.label,
            base_url=base_url,
            model=model,
            api_key=_encrypt(self.deps, "api_key", user_id, row_id, api_key),
            api_key_last4=last4(api_key),
            embedding_base_url=embedding_base_url,
            embedding_model=_clean(data.embedding_model),
            embedding_api_key=(
                _encrypt(self.deps, "embedding_api_key", user_id, row_id, embedding_key)
                if embedding_key
                else None
            ),
            is_active=False,
            created_at=now,
            updated_at=now,
        )
        _validate_embedding_route(config)
        async with self.deps.uow_factory(user_id) as uow:
            existing = await uow.llm_configs.list(user_id)
            if data.activate or not existing:
                await uow.llm_configs.deactivate_all(user_id)
                config.is_active = True
            await uow.llm_configs.add(config)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="llm.config_created",
                entity=TABLE,
                entity_id=row_id,
                metadata={"kind": data.kind.value},
            )
            await _emit_config_changed(uow, user_id, row_id)
            await uow.commit()
        return ProviderConfigView.of(config, None)


def _validate_embedding_route(config: ProviderConfig) -> None:
    try:
        choose_embedding_route(
            kind=config.kind,
            chat_base_url=config.base_url,
            embedding_base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            has_embedding_key=config.embedding_api_key is not None,
        )
    except Exception as exc:
        raise ValidationError(str(exc)) from exc


@dataclass(slots=True)
class UpdateProviderConfig:
    deps: LLMDeps

    async def execute(
        self, user_id: uuid.UUID, config_id: uuid.UUID, patch: ProviderConfigPatch
    ) -> ProviderConfigView:
        async with self.deps.uow_factory(user_id) as uow:
            config = await uow.llm_configs.get(user_id, config_id)
            if config is None:
                raise NotFoundError("provider config not found")
            if patch.label is not None:
                config.label = patch.label.strip() or config.label
            if patch.base_url is not None and config.kind is ProviderKind.CUSTOM:
                config.base_url = await _resolve_base_url(self.deps, config.kind, patch.base_url)
            if patch.model is not None and patch.model.strip():
                config.model = patch.model.strip()
            if patch.api_key is not None and patch.api_key.strip():
                key = patch.api_key.strip()
                config.api_key = _encrypt(self.deps, "api_key", user_id, config.id, key)
                config.api_key_last4 = last4(key)
            if "embedding_base_url" in patch.fields_set:
                url = _clean(patch.embedding_base_url)
                config.embedding_base_url = (
                    (await self.deps.url_guard.validate(url)).rstrip("/") if url else None
                )
            if "embedding_model" in patch.fields_set:
                config.embedding_model = _clean(patch.embedding_model)
            if "embedding_api_key" in patch.fields_set:
                emb_key = _clean(patch.embedding_api_key)
                config.embedding_api_key = (
                    _encrypt(self.deps, "embedding_api_key", user_id, config.id, emb_key)
                    if emb_key
                    else None
                )
            _validate_embedding_route(config)
            config.updated_at = self.deps.clock.now()
            await uow.llm_configs.update(config)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="llm.config_updated",
                entity=TABLE,
                entity_id=config.id,
            )
            await _emit_config_changed(uow, user_id, config.id)
            last = await uow.connection_checks.latest(user_id, config.id)
            await uow.commit()
        return ProviderConfigView.of(config, last)


@dataclass(slots=True)
class ListProviderConfigs:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> list[ProviderConfigView]:
        async with self.uow_factory(user_id) as uow:
            configs = await uow.llm_configs.list(user_id)
            return [
                ProviderConfigView.of(c, await uow.connection_checks.latest(user_id, c.id))
                for c in configs
            ]


@dataclass(slots=True)
class ActivateProviderConfig:
    deps: LLMDeps

    async def execute(self, user_id: uuid.UUID, config_id: uuid.UUID) -> ProviderConfigView:
        async with self.deps.uow_factory(user_id) as uow:
            config = await uow.llm_configs.get(user_id, config_id)
            if config is None:
                raise NotFoundError("provider config not found")
            await uow.llm_configs.deactivate_all(user_id)
            config.is_active = True
            config.updated_at = self.deps.clock.now()
            await uow.llm_configs.update(config)
            await _emit_config_changed(uow, user_id, config.id)
            last = await uow.connection_checks.latest(user_id, config.id)
            await uow.commit()
        return ProviderConfigView.of(config, last)


@dataclass(slots=True)
class DeleteProviderConfig:
    deps: LLMDeps

    async def execute(self, user_id: uuid.UUID, config_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            if not await uow.llm_configs.delete(user_id, config_id):
                raise NotFoundError("provider config not found")
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="llm.config_deleted",
                entity=TABLE,
                entity_id=config_id,
            )
            await _emit_config_changed(uow, user_id, config_id)
            await uow.commit()


@dataclass(slots=True)
class VerifyConnection:
    """Live handshake against the provider; persists the result and emits `llm.status`."""

    deps: LLMDeps

    async def execute(
        self, user_id: uuid.UUID, config_id: uuid.UUID | None = None
    ) -> tuple[ProviderConfigView, ConnectionCheck]:
        async with self.deps.uow_factory(user_id) as uow:
            config = await (
                uow.llm_configs.get(user_id, config_id)
                if config_id
                else uow.llm_configs.get_active(user_id)
            )
        if config is None:
            raise NotFoundError("no provider configured")
        if config.kind is ProviderKind.CUSTOM:
            await self.deps.url_guard.validate(config.base_url)  # re-check (DNS may change)
        api_key = self.deps.cipher.decrypt(
            config.api_key, aad(TABLE, "api_key", user_id, config.id)
        ).decode()
        provider = self.deps.provider_factory.create(
            base_url=config.base_url, api_key=api_key, model=config.model
        )
        del api_key
        result = await provider.handshake()
        check = ConnectionCheck(
            id=new_uuid7(),
            user_id=user_id,
            provider_config_id=config.id,
            ok=result.ok,
            latency_ms=result.latency_ms,
            model=result.model,
            model_listed=result.model_listed,
            error_code=result.error_code.value if result.error_code else None,
            error_message=result.error_message,
            checked_at=self.deps.clock.now(),
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.connection_checks.add(check)
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type=EventType.LLM_STATUS,
                    payload=connection_status_payload(config, check),
                )
            )
            await uow.commit()
        return ProviderConfigView.of(config, check), check


def connection_status_payload(
    config: ProviderConfig | ProviderConfigView, check: ConnectionCheck | None
) -> dict[str, object]:
    return {
        "provider_config_id": str(config.id),
        "kind": config.kind.value,
        "label": config.label,
        "model": config.model,
        "ok": check.ok if check else None,
        "latency_ms": check.latency_ms if check else None,
        "error_code": check.error_code if check else None,
        "checked_at": check.checked_at.isoformat() if check else None,
    }


@dataclass(slots=True)
class GetConnectionStatus:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> ProviderConfigView | None:
        async with self.uow_factory(user_id) as uow:
            config = await uow.llm_configs.get_active(user_id)
            if config is None:
                return None
            return ProviderConfigView.of(
                config, await uow.connection_checks.latest(user_id, config.id)
            )


@dataclass(slots=True)
class ResolveChatProvider:
    """The chat provider for the user's ACTIVE config, bound to the exact model they saved."""

    deps: LLMDeps

    async def execute(self, user_id: uuid.UUID) -> ILLMProvider:
        async with self.deps.uow_factory(user_id) as uow:
            config = await uow.llm_configs.get_active(user_id)
        if config is None:
            raise ValidationError(
                "configure an LLM provider and model in LLM Gateway before running discovery"
            )
        if config.kind is ProviderKind.CUSTOM:
            await self.deps.url_guard.validate(config.base_url)
        key = self.deps.cipher.decrypt(
            config.api_key, aad(TABLE, "api_key", user_id, config.id)
        ).decode()
        return self.deps.provider_factory.create(
            base_url=config.base_url, api_key=key, model=config.model
        )


@dataclass(slots=True)
class FetchModels:
    """List the models the user's key can access (`GET {base_url}/models`). Purely a picker aid:
    nothing is selected or saved automatically."""

    deps: LLMDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        *,
        config_id: uuid.UUID | None = None,
        kind: ProviderKind | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> tuple[str, list[str]]:
        if config_id is not None:
            async with self.deps.uow_factory(user_id) as uow:
                config = await uow.llm_configs.get(user_id, config_id)
            if config is None:
                raise NotFoundError("provider config not found")
            if config.kind is ProviderKind.CUSTOM:
                await self.deps.url_guard.validate(config.base_url)
            resolved_base = config.base_url
            key = (api_key or "").strip() or self.deps.cipher.decrypt(
                config.api_key, aad(TABLE, "api_key", user_id, config.id)
            ).decode()
        else:
            if kind is None or not (api_key or "").strip():
                raise ValidationError("kind and api_key are required to load models")
            resolved_base = await _resolve_base_url(self.deps, kind, base_url)
            key = (api_key or "").strip()
        provider = self.deps.provider_factory.create(base_url=resolved_base, api_key=key, model="")
        del key
        models = await provider.list_models()
        return resolved_base, sorted(set(models))


@dataclass(frozen=True, slots=True)
class PromptTestResult:
    provider_config_id: uuid.UUID
    model: str
    text: str
    reasoning: str | None
    finish_reason: str | None
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(slots=True)
class RunTestPrompt:
    """Send a real prompt through the active (or given) provider to see the model generate."""

    deps: LLMDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        prompt: str,
        *,
        max_tokens: int,
        config_id: uuid.UUID | None = None,
    ) -> PromptTestResult:
        if not prompt.strip():
            raise ValidationError("prompt is empty")
        async with self.deps.uow_factory(user_id) as uow:
            config = await (
                uow.llm_configs.get(user_id, config_id)
                if config_id
                else uow.llm_configs.get_active(user_id)
            )
        if config is None:
            raise NotFoundError("no provider configured")
        if config.kind is ProviderKind.CUSTOM:
            await self.deps.url_guard.validate(config.base_url)
        api_key = self.deps.cipher.decrypt(
            config.api_key, aad(TABLE, "api_key", user_id, config.id)
        ).decode()
        provider = self.deps.provider_factory.create(
            base_url=config.base_url, api_key=api_key, model=config.model
        )
        del api_key
        started = time.perf_counter()
        completion = await provider.complete(
            [ChatMessage(role="user", content=prompt)], max_tokens=max_tokens, temperature=0.3
        )
        return PromptTestResult(
            provider_config_id=config.id,
            model=completion.model,
            text=completion.text,
            reasoning=completion.reasoning,
            finish_reason=completion.finish_reason,
            latency_ms=round((time.perf_counter() - started) * 1000),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )


@dataclass(slots=True)
class GetEmbeddingProvider:
    """Embedding provider for the user's active config, independent of the chat provider."""

    deps: LLMDeps

    async def execute(self, user_id: uuid.UUID) -> IEmbeddingProvider:
        async with self.deps.uow_factory(user_id) as uow:
            config = await uow.llm_configs.get_active(user_id)
        if config is None:
            route = choose_embedding_route(
                kind=ProviderKind.CUSTOM,
                chat_base_url="",
                embedding_base_url=None,
                embedding_model=None,
                has_embedding_key=False,
            )
            return self.deps.embedding_factory.create(route, None)
        route = choose_embedding_route(
            kind=config.kind,
            chat_base_url=config.base_url,
            embedding_base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            has_embedding_key=config.embedding_api_key is not None,
        )
        key: str | None = None
        if route.key_source is EmbeddingKeySource.EMBEDDING and config.embedding_api_key:
            key = self.deps.cipher.decrypt(
                config.embedding_api_key, aad(TABLE, "embedding_api_key", user_id, config.id)
            ).decode()
        elif route.key_source is EmbeddingKeySource.CHAT:
            key = self.deps.cipher.decrypt(
                config.api_key, aad(TABLE, "api_key", user_id, config.id)
            ).decode()
        return self.deps.embedding_factory.create(route, key)


__all__ = [
    "ActivateProviderConfig",
    "CreateProviderConfig",
    "DeleteProviderConfig",
    "FetchModels",
    "GetConnectionStatus",
    "GetEmbeddingProvider",
    "LLMDeps",
    "ListProviderConfigs",
    "PromptTestResult",
    "ProviderConfigInput",
    "ProviderConfigPatch",
    "ProviderConfigView",
    "ResolveChatProvider",
    "RunTestPrompt",
    "UpdateProviderConfig",
    "VerifyConnection",
    "connection_status_payload",
]
