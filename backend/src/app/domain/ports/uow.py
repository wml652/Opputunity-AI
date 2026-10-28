from __future__ import annotations

import uuid
from types import TracebackType
from typing import Protocol, Self

from app.domain.ports.applications import (
    IApplicationDetailRepository,
    IChannelRepository,
    ICheckpointRepository,
    IEssayDraftRepository,
    IFieldRepository,
    INotificationRepository,
    IQuestionRepository,
)
from app.domain.ports.discovery import (
    IApplicationRepository,
    ICampaignRepository,
    ICrawledPageRepository,
    IOpportunityRepository,
    IPlaybookRepository,
    IVerdictRepository,
)
from app.domain.ports.repositories import (
    IAuditLog,
    IConnectionCheckRepository,
    IEventOutbox,
    ILLMConfigRepository,
    IRefreshTokenRepository,
    IUserRepository,
)
from app.domain.ports.vault import (
    IDocumentRepository,
    IEssayRepository,
    IFingerprintRepository,
    IMemoryRepository,
    IProfileRepository,
)


class IUnitOfWork(Protocol):
    """One database transaction. Leaving the block without `commit()` rolls back."""

    @property
    def users(self) -> IUserRepository: ...

    @property
    def refresh_tokens(self) -> IRefreshTokenRepository: ...

    @property
    def llm_configs(self) -> ILLMConfigRepository: ...

    @property
    def connection_checks(self) -> IConnectionCheckRepository: ...

    @property
    def events(self) -> IEventOutbox: ...

    @property
    def audit(self) -> IAuditLog: ...

    @property
    def profiles(self) -> IProfileRepository: ...

    @property
    def documents(self) -> IDocumentRepository: ...

    @property
    def essays(self) -> IEssayRepository: ...

    @property
    def fingerprints(self) -> IFingerprintRepository: ...

    @property
    def memory(self) -> IMemoryRepository: ...

    @property
    def campaigns(self) -> ICampaignRepository: ...

    @property
    def pages(self) -> ICrawledPageRepository: ...

    @property
    def opportunities(self) -> IOpportunityRepository: ...

    @property
    def verdicts(self) -> IVerdictRepository: ...

    @property
    def applications(self) -> IApplicationRepository: ...

    @property
    def playbooks(self) -> IPlaybookRepository: ...

    @property
    def app_details(self) -> IApplicationDetailRepository: ...

    @property
    def fields(self) -> IFieldRepository: ...

    @property
    def questions(self) -> IQuestionRepository: ...

    @property
    def checkpoints(self) -> ICheckpointRepository: ...

    @property
    def essay_drafts(self) -> IEssayDraftRepository: ...

    @property
    def channels(self) -> IChannelRepository: ...

    @property
    def notifications(self) -> INotificationRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def bind_user(self, user_id: uuid.UUID) -> None:
        """Set the tenant for row-level security for the rest of this transaction."""
        ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class IUnitOfWorkFactory(Protocol):
    def __call__(self, user_id: uuid.UUID | None = None) -> IUnitOfWork: ...
