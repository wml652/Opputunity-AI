from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from app.domain.events.models import NewEvent, PipelineEvent
from app.domain.identity.models import RefreshToken, User
from app.domain.llm.provider import ConnectionCheck, ProviderConfig
from app.domain.shared.enums import Actor


class IUserRepository(Protocol):
    async def add(self, user: User) -> None: ...

    async def get(self, user_id: uuid.UUID) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def update_password_hash(self, user_id: uuid.UUID, password_hash: str) -> None: ...


class IRefreshTokenRepository(Protocol):
    async def add(self, token: RefreshToken) -> None: ...

    async def get(self, user_id: uuid.UUID, token_id: uuid.UUID) -> RefreshToken | None: ...

    async def revoke(
        self,
        user_id: uuid.UUID,
        token_id: uuid.UUID,
        at: datetime,
        replaced_by: uuid.UUID | None,
    ) -> None: ...

    async def revoke_family(
        self, user_id: uuid.UUID, family_id: uuid.UUID, at: datetime
    ) -> int: ...


class ILLMConfigRepository(Protocol):
    async def add(self, config: ProviderConfig) -> None: ...

    async def get(self, user_id: uuid.UUID, config_id: uuid.UUID) -> ProviderConfig | None: ...

    async def get_active(self, user_id: uuid.UUID) -> ProviderConfig | None: ...

    async def list(self, user_id: uuid.UUID) -> list[ProviderConfig]: ...

    async def update(self, config: ProviderConfig) -> None: ...

    async def delete(self, user_id: uuid.UUID, config_id: uuid.UUID) -> bool: ...

    async def deactivate_all(self, user_id: uuid.UUID) -> None: ...


class IConnectionCheckRepository(Protocol):
    async def add(self, check: ConnectionCheck) -> None: ...

    async def latest(
        self, user_id: uuid.UUID, provider_config_id: uuid.UUID
    ) -> ConnectionCheck | None: ...


class IEventOutbox(Protocol):
    async def append(self, event: NewEvent) -> PipelineEvent: ...

    async def list_since(
        self, user_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[PipelineEvent]: ...

    async def claim_unpublished(self, limit: int) -> list[PipelineEvent]:
        """System scope only: lock unpublished rows (SKIP LOCKED) for the relay."""
        ...

    async def mark_published(self, event_ids: Sequence[uuid.UUID], at: datetime) -> None: ...


class IAuditLog(Protocol):
    async def record(
        self,
        *,
        user_id: uuid.UUID,
        actor: Actor,
        action: str,
        entity: str,
        entity_id: uuid.UUID | None = None,
        ip: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...
