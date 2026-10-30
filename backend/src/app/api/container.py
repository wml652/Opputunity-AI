"""What the HTTP adapter needs, expressed as ports and use cases. Filled by `app.bootstrap`."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.application.applications.submission import (
    ApproveSubmission,
    GetReceiptScreenshot,
    GetVerification,
    RejectApplication,
)
from app.application.applications.use_cases import (
    AnswerQuestion,
    BatchPrepare,
    GetApplicationDetail,
    GetCheckpointScreenshot,
    GetInbox,
    StartFilling,
)
from app.application.auth.use_cases import (
    EnsureDevUser,
    GetCurrentUser,
    LoginUser,
    Logout,
    RefreshSession,
    RegisterUser,
)
from app.application.discovery.sessions import (
    CaptureBrowserSession,
    DeleteBrowserSession,
    DeleteSearchCredential,
    ListBrowserSessions,
    ListSearchCredentials,
    SaveSearchCredential,
)
from app.application.discovery.use_cases import (
    CancelCampaign,
    CreateCampaign,
    GetCampaign,
    GetOpportunity,
    GetPage,
    GetPipelineSummary,
    ListApplicationQueue,
    ListCampaigns,
    ListOpportunities,
    RetryPage,
)
from app.application.essay.synthesis import EditEssayDraft, ListEssayDrafts, SynthesizeEssay
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
from app.application.notifications.use_cases import (
    DeleteChannel,
    ListChannels,
    RecentNotifications,
    RecordDeliveryReceipt,
    SaveChannel,
    SendTestNotification,
    VerifyChannel,
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
    retry_page: RetryPage
    get_page: GetPage
    log_path: Callable[[uuid.UUID], str]
    spawn: Callable[[Coroutine[Any, Any, None]], None]


@dataclass(frozen=True, slots=True)
class SessionUseCases:
    list_sessions: ListBrowserSessions
    capture: CaptureBrowserSession
    delete_session: DeleteBrowserSession
    list_search: ListSearchCredentials
    save_search: SaveSearchCredential
    delete_search: DeleteSearchCredential
    headful_available: bool


@dataclass(frozen=True, slots=True)
class ApplicationUseCases:
    detail: GetApplicationDetail
    start: StartFilling
    inbox: GetInbox
    answer: AnswerQuestion
    screenshot: GetCheckpointScreenshot
    synthesize: SynthesizeEssay
    edit_draft: EditEssayDraft
    list_drafts: ListEssayDrafts
    verification: GetVerification
    batch: BatchPrepare
    approve: ApproveSubmission
    reject: RejectApplication
    receipt: GetReceiptScreenshot


@dataclass(frozen=True, slots=True)
class NotificationUseCases:
    save_channel: SaveChannel
    verify_channel: VerifyChannel
    list_channels: ListChannels
    delete_channel: DeleteChannel
    send_test: SendTestNotification
    recent: RecentNotifications
    receipt: RecordDeliveryReceipt
    twilio_auth_token: str | None
    meta_app_secret: str | None
    meta_verify_token: str | None
    public_api_url: str
    providers: dict[str, str] = field(default_factory=dict)


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
    applications: ApplicationUseCases
    notifications: NotificationUseCases
    sessions: SessionUseCases
