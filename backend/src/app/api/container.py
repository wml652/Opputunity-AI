"""What the HTTP adapter needs, expressed as ports and use cases. Filled by `app.bootstrap`."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.application.auth.use_cases import (
    EnsureDevUser,
    GetCurrentUser,
    LoginUser,
    Logout,
    RefreshSession,
    RegisterUser,
)
from app.application.discovery.use_cases import (
    CancelCampaign,
    CreateCampaign,
    GetCampaign,
    GetOpportunity,
    GetPipelineSummary,
    ListApplicationQueue,
    ListCampaigns,
    ListOpportunities,
)
from app.application.essay.use_cases import (
    CreateEssay,
    DeleteEssay,
    GetFingerprint,
    ListEssays,
    UpdateEssay,
)
from app.application.events.use_cases import ReplayEvents
from app.application.llm.use_cases import (
    ActivateProviderConfig,
    CreateProviderConfig,
    DeleteProviderConfig,
    FetchModels,
    GetConnectionStatus,
    ListProviderConfigs,
    RunTestPrompt,
    UpdateProviderConfig,
    VerifyConnection,
)
from app.application.memory.use_cases import (
    DeleteMemory,
    LearnAnswer,
    ListMemory,
    RecallMemory,
    UpdateMemory,
)
from app.application.profile.use_cases import (
    DeleteDocument,
    DownloadDocument,
    GetDocumentText,
    GetProfile,
    ListDocuments,
    SaveProfile,
    UploadDocument,
)
from app.application.system.use_cases import GetWorkerStatus
from app.domain.ports.discovery import ICampaignRunner
from app.domain.ports.events import IEventBus
from app.domain.ports.security import IAccessTokenService


@dataclass(frozen=True, slots=True)
class ApiConfig:
    cookie_secure: bool
    cookie_domain: str | None
    access_ttl_seconds: int
    refresh_ttl_seconds: int
    cors_origins: list[str] = field(default_factory=list)
    sse_heartbeat_seconds: float = 15.0
    expose_docs: bool = True
    dev_auth_bypass: bool = False


@dataclass(frozen=True, slots=True)
class VaultUseCases:
    get_profile: GetProfile
    save_profile: SaveProfile
    upload_document: UploadDocument
    list_documents: ListDocuments
    document_text: GetDocumentText
    download_document: DownloadDocument
    delete_document: DeleteDocument
    list_essays: ListEssays
    create_essay: CreateEssay
    update_essay: UpdateEssay
    delete_essay: DeleteEssay
    get_fingerprint: GetFingerprint
    list_memory: ListMemory
    learn_answer: LearnAnswer
    update_memory: UpdateMemory
    delete_memory: DeleteMemory
    recall: RecallMemory


@dataclass(frozen=True, slots=True)
class DiscoveryUseCases:
    create_campaign: CreateCampaign
    list_campaigns: ListCampaigns
    get_campaign: GetCampaign
    cancel_campaign: CancelCampaign
    list_opportunities: ListOpportunities
    get_opportunity: GetOpportunity
    queue: ListApplicationQueue
    summary: GetPipelineSummary
    runner: ICampaignRunner


@dataclass(frozen=True, slots=True)
class ApiContainer:
    config: ApiConfig
    access_tokens: IAccessTokenService
    event_bus: IEventBus
    register_user: RegisterUser
    login_user: LoginUser
    refresh_session: RefreshSession
    logout: Logout
    get_current_user: GetCurrentUser
    ensure_dev_user: EnsureDevUser
    create_provider: CreateProviderConfig
    update_provider: UpdateProviderConfig
    list_providers: ListProviderConfigs
    activate_provider: ActivateProviderConfig
    delete_provider: DeleteProviderConfig
    verify_connection: VerifyConnection
    test_prompt: RunTestPrompt
    fetch_models: FetchModels
    connection_status: GetConnectionStatus
    replay_events: ReplayEvents
    worker_status: GetWorkerStatus
    vault: VaultUseCases
    discovery: DiscoveryUseCases
