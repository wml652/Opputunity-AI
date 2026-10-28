from __future__ import annotations

import uuid
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.repositories.applications import (
    SqlApplicationDetailRepository,
    SqlChannelRepository,
    SqlCheckpointRepository,
    SqlEssayDraftRepository,
    SqlFieldRepository,
    SqlNotificationRepository,
    SqlQuestionRepository,
)
from app.infrastructure.db.repositories.discovery import (
    SqlApplicationRepository,
    SqlCampaignRepository,
    SqlCrawledPageRepository,
    SqlOpportunityRepository,
    SqlPlaybookRepository,
    SqlVerdictRepository,
)
from app.infrastructure.db.repositories.events import SqlAuditLog, SqlEventOutbox
from app.infrastructure.db.repositories.identity import (
    SqlRefreshTokenRepository,
    SqlUserRepository,
)
from app.infrastructure.db.repositories.llm import (
    SqlConnectionCheckRepository,
    SqlLLMConfigRepository,
)
from app.infrastructure.db.repositories.vault import (
    SqlDocumentRepository,
    SqlEssayRepository,
    SqlFingerprintRepository,
    SqlMemoryRepository,
    SqlProfileRepository,
)

_SET_TENANT = text("SELECT set_config('app.user_id', :uid, true)")
_SET_SYSTEM = text("SELECT set_config('app.system', 'on', true)")


class SqlAlchemyUnitOfWork:
    """A single transaction. Tenant context is applied with `set_config(..., is_local => true)`,
    i.e. `SET LOCAL`, so it can never leak to another request through the connection pool."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID | None = None,
        *,
        system: bool = False,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._user_id = user_id
        self._system = system
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session

    async def __aenter__(self) -> Self:
        session = self._sessionmaker()
        await session.begin()
        self._session = session
        if self._system:
            await session.execute(_SET_SYSTEM)
        if self._user_id is not None:
            await session.execute(_SET_TENANT, {"uid": str(self._user_id)})
        self.users = SqlUserRepository(session)
        self.refresh_tokens = SqlRefreshTokenRepository(session)
        self.llm_configs = SqlLLMConfigRepository(session)
        self.connection_checks = SqlConnectionCheckRepository(session)
        self.events = SqlEventOutbox(session)
        self.audit = SqlAuditLog(session)
        self.profiles = SqlProfileRepository(session)
        self.documents = SqlDocumentRepository(session)
        self.essays = SqlEssayRepository(session)
        self.fingerprints = SqlFingerprintRepository(session)
        self.memory = SqlMemoryRepository(session)
        self.campaigns = SqlCampaignRepository(session)
        self.pages = SqlCrawledPageRepository(session)
        self.opportunities = SqlOpportunityRepository(session)
        self.verdicts = SqlVerdictRepository(session)
        self.applications = SqlApplicationRepository(session)
        self.playbooks = SqlPlaybookRepository(session)
        self.app_details = SqlApplicationDetailRepository(session)
        self.fields = SqlFieldRepository(session)
        self.questions = SqlQuestionRepository(session)
        self.checkpoints = SqlCheckpointRepository(session)
        self.essay_drafts = SqlEssayDraftRepository(session)
        self.channels = SqlChannelRepository(session)
        self.notifications = SqlNotificationRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if session.in_transaction():
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def bind_user(self, user_id: uuid.UUID) -> None:
        self._user_id = user_id
        await self.session.execute(_SET_TENANT, {"uid": str(user_id)})

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    def __call__(self, user_id: uuid.UUID | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._sessionmaker, user_id)


class SystemUnitOfWorkFactory:
    """Cross-tenant scope (`app.system = 'on'`) for the outbox relay and cron sweeps ONLY."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    def __call__(self, user_id: uuid.UUID | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._sessionmaker, user_id, system=True)
