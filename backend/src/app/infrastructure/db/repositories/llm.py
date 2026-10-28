from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.llm.provider import ConnectionCheck, EncryptedSecret, ProviderConfig
from app.infrastructure.db.models import LLMConnectionCheckRow, LLMProviderConfigRow


def _config(row: LLMProviderConfigRow) -> ProviderConfig:
    embedding_key = None
    if (
        row.embedding_api_key_ciphertext is not None
        and row.embedding_api_key_nonce is not None
        and row.embedding_key_version is not None
    ):
        embedding_key = EncryptedSecret(
            ciphertext=row.embedding_api_key_ciphertext,
            nonce=row.embedding_api_key_nonce,
            key_version=row.embedding_key_version,
        )
    return ProviderConfig(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        label=row.label,
        base_url=row.base_url,
        model=row.model,
        api_key=EncryptedSecret(
            ciphertext=row.api_key_ciphertext, nonce=row.api_key_nonce, key_version=row.key_version
        ),
        api_key_last4=row.api_key_last4,
        embedding_base_url=row.embedding_base_url,
        embedding_model=row.embedding_model,
        embedding_api_key=embedding_key,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _columns(config: ProviderConfig) -> dict[str, object]:
    emb = config.embedding_api_key
    return {
        "kind": config.kind,
        "label": config.label,
        "base_url": config.base_url,
        "model": config.model,
        "api_key_ciphertext": config.api_key.ciphertext,
        "api_key_nonce": config.api_key.nonce,
        "key_version": config.api_key.key_version,
        "api_key_last4": config.api_key_last4,
        "embedding_base_url": config.embedding_base_url,
        "embedding_model": config.embedding_model,
        "embedding_api_key_ciphertext": emb.ciphertext if emb else None,
        "embedding_api_key_nonce": emb.nonce if emb else None,
        "embedding_key_version": emb.key_version if emb else None,
        "is_active": config.is_active,
        "updated_at": config.updated_at,
    }


def _check(row: LLMConnectionCheckRow) -> ConnectionCheck:
    return ConnectionCheck(
        id=row.id,
        user_id=row.user_id,
        provider_config_id=row.provider_config_id,
        ok=row.ok,
        latency_ms=row.latency_ms,
        model=row.model,
        model_listed=row.model_listed,
        error_code=row.error_code,
        error_message=row.error_message,
        checked_at=row.checked_at,
    )


class SqlLLMConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, config: ProviderConfig) -> None:
        self._s.add(
            LLMProviderConfigRow(
                id=config.id,
                user_id=config.user_id,
                created_at=config.created_at,
                **_columns(config),
            )
        )
        await self._s.flush()

    async def get(self, user_id: uuid.UUID, config_id: uuid.UUID) -> ProviderConfig | None:
        row = await self._s.scalar(
            select(LLMProviderConfigRow).where(
                LLMProviderConfigRow.user_id == user_id, LLMProviderConfigRow.id == config_id
            )
        )
        return _config(row) if row else None

    async def get_active(self, user_id: uuid.UUID) -> ProviderConfig | None:
        row = await self._s.scalar(
            select(LLMProviderConfigRow).where(
                LLMProviderConfigRow.user_id == user_id, LLMProviderConfigRow.is_active.is_(True)
            )
        )
        return _config(row) if row else None

    async def list(self, user_id: uuid.UUID) -> list[ProviderConfig]:
        rows = await self._s.scalars(
            select(LLMProviderConfigRow)
            .where(LLMProviderConfigRow.user_id == user_id)
            .order_by(LLMProviderConfigRow.created_at)
        )
        return [_config(r) for r in rows]

    async def update(self, config: ProviderConfig) -> None:
        await self._s.execute(
            update(LLMProviderConfigRow)
            .where(
                LLMProviderConfigRow.user_id == config.user_id,
                LLMProviderConfigRow.id == config.id,
            )
            .values(**_columns(config))
        )

    async def delete(self, user_id: uuid.UUID, config_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(LLMProviderConfigRow).where(
                LLMProviderConfigRow.user_id == user_id, LLMProviderConfigRow.id == config_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def deactivate_all(self, user_id: uuid.UUID) -> None:
        await self._s.execute(
            update(LLMProviderConfigRow)
            .where(
                LLMProviderConfigRow.user_id == user_id, LLMProviderConfigRow.is_active.is_(True)
            )
            .values(is_active=False)
        )


class SqlConnectionCheckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, check: ConnectionCheck) -> None:
        self._s.add(
            LLMConnectionCheckRow(
                id=check.id,
                user_id=check.user_id,
                provider_config_id=check.provider_config_id,
                ok=check.ok,
                latency_ms=check.latency_ms,
                model=check.model,
                model_listed=check.model_listed,
                error_code=check.error_code,
                error_message=check.error_message,
                checked_at=check.checked_at,
            )
        )
        await self._s.flush()

    async def latest(
        self, user_id: uuid.UUID, provider_config_id: uuid.UUID
    ) -> ConnectionCheck | None:
        row = await self._s.scalar(
            select(LLMConnectionCheckRow)
            .where(
                LLMConnectionCheckRow.user_id == user_id,
                LLMConnectionCheckRow.provider_config_id == provider_config_id,
            )
            .order_by(LLMConnectionCheckRow.checked_at.desc())
            .limit(1)
        )
        return _check(row) if row else None
