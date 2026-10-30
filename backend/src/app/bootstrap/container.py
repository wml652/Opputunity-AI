"""Composition root: the only place that knows about concrete adapters."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.container import (
    ApiConfig,
    ApiContainer,
    ApplicationUseCases,
    DiscoveryUseCases,
    NotificationUseCases,
    SessionUseCases,
    VaultUseCases,
)
from app.application.applications.submission import (
    ApproveSubmission,
    GetReceiptScreenshot,
    GetVerification,
    ReapLeases,
    RejectApplication,
    RunSubmission,
    SweepDeadlines,
)
from app.application.applications.use_cases import (
    AnswerQuestion,
    ApplicationDeps,
    BatchPrepare,
    GetApplicationDetail,
    GetCheckpointScreenshot,
    GetInbox,
    RunApplication,
    StartFilling,
)
from app.application.auth.use_cases import (
    AuthDeps,
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
    SessionDeps,
)
from app.application.discovery.use_cases import (
    CancelCampaign,
    CreateCampaign,
    DiscoveryDeps,
    GetCampaign,
    GetOpportunity,
    GetPage,
    GetPipelineSummary,
    ListApplicationQueue,
    ListCampaigns,
    ListOpportunities,
    RetryPage,
    RunCampaign,
    SweepInterruptedCampaigns,
)
from app.application.essay.synthesis import (
    EditEssayDraft,
    EssayDeps,
    ListEssayDrafts,
    SynthesizeEssay,
)
from app.application.essay.use_cases import (
    CreateEssay,
    DeleteEssay,
    GetFingerprint,
    ListEssays,
    UpdateEssay,
)
from app.application.events.use_cases import RelayOutbox, ReplayEvents
from app.application.llm.use_cases import (
    ActivateProviderConfig,
    CreateProviderConfig,
    DeleteProviderConfig,
    FetchModels,
    GetConnectionStatus,
    GetEmbeddingProvider,
    ListProviderConfigs,
    LLMDeps,
    ResolveChatProvider,
    ResolveRerankProvider,
    ResolveVisionProvider,
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
    NotificationDeps,
    NotifyUser,
    RecentNotifications,
    RecordDeliveryReceipt,
    SaveChannel,
    SendTestNotification,
    VerifyChannel,
)
from app.application.profile.indexing import IndexDocument, IndexMissingDocuments
from app.application.profile.use_cases import (
    DeleteDocument,
    DownloadDocument,
    GetDocumentText,
    GetProfile,
    ListDocuments,
    SaveProfile,
    UploadDocument,
)
from app.application.services.evidence import EvidenceDeps, ProfileEvidenceRetriever
from app.application.system.use_cases import GetWorkerStatus
from app.application.vault_deps import VaultDeps
from app.domain.ports.applications import IApplicationRunner, INotifier
from app.domain.ports.discovery import ICampaignRunner, IHeadfulSessionCapturer
from app.domain.ports.llm import (
    IRerankProvider,
    IRerankProviderFactory,
    IVisionProvider,
    IVisionProviderFactory,
)
from app.domain.shared.clock import Clock, SystemClock
from app.domain.shared.enums import NotifChannel
from app.infrastructure.browser.form_filler import PlaywrightFormFiller
from app.infrastructure.browser.session_capture import PlaywrightSessionCapturer
from app.infrastructure.config.settings import Settings
from app.infrastructure.crypto.aes_gcm_cipher import AesGcmCipher
from app.infrastructure.crypto.key_ring import KeyRing
from app.infrastructure.db.session import create_engine, create_sessionmaker
from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory, SystemUnitOfWorkFactory
from app.infrastructure.documents.pdf import PypdfTextExtractor
from app.infrastructure.events.campaign_log import FileCampaignLog
from app.infrastructure.events.redis_bus import RedisEventBus, RedisWorkerRegistry
from app.infrastructure.extraction.evidence_answerer import LlmEvidenceAnswerer
from app.infrastructure.extraction.llm_adapters import (
    LlmFuzzyEligibilityEvaluator,
    LlmOpportunityExtractor,
)
from app.infrastructure.linkedin.authenticated_adapter import LinkedInAuthenticatedAdapter
from app.infrastructure.linkedin.public_adapter import LinkedInPublicAdapter
from app.infrastructure.llm.embeddings import EmbeddingProviderFactory
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProviderFactory
from app.infrastructure.notify.notifiers import (
    DevLogNotifier,
    MetaWhatsAppNotifier,
    ResendNotifier,
    SmtpNotifier,
    TwilioWhatsAppNotifier,
)
from app.infrastructure.runners.application_runner import InProcessApplicationRunner
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from app.infrastructure.scraping.distiller import HtmlContentProcessor
from app.infrastructure.scraping.document_parser import PdfLayoutParser
from app.infrastructure.scraping.fetchers import playwright_available
from app.infrastructure.scraping.site_crawler import SiteCrawlerFactory
from app.infrastructure.scraping.ssrf_guard import Resolver, SsrfGuard
from app.infrastructure.search.providers import SearchChainFactory, build_search_chain
from app.infrastructure.security.passwords import Argon2PasswordHasher
from app.infrastructure.security.tokens import JwtAccessTokenService, RefreshTokenCodec
from app.infrastructure.storage.local_fs import LocalFileStorage
from app.infrastructure.stylometry.metrics import StylometryAnalyzer


@dataclass(slots=True)
class Infrastructure:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    uow_factory: SqlAlchemyUnitOfWorkFactory
    system_uow_factory: SystemUnitOfWorkFactory
    cipher: AesGcmCipher
    llm_factory: OpenAICompatibleProviderFactory
    embedding_factory: EmbeddingProviderFactory
    url_guard: SsrfGuard
    event_bus: RedisEventBus
    worker_registry: RedisWorkerRegistry
    storage: LocalFileStorage
    crawl_guard: SsrfGuard
    clock: Clock
    campaign_runner: ICampaignRunner | None = None
    application_runner: IApplicationRunner | None = None
    session_capturer: IHeadfulSessionCapturer | None = None  # tests inject a fake
    rerank_factory: IRerankProviderFactory | None = None  # tests inject a fake
    vision_factory: IVisionProviderFactory | None = None  # tests inject a fake
    background: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Fire-and-forget background work owned by the process (page retries)."""
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def aclose(self) -> None:
        for runner in (self.campaign_runner, self.application_runner):
            closer = getattr(runner, "aclose", None)
            if closer is not None:
                await closer()
        await self.llm_factory.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def _connect_redis(url: str) -> Redis:
    if url.startswith("memory://"):
        # Single-process dev mode: in-memory Redis shared by the API and its in-process relay.
        # Separate worker processes cannot see it (validate_for_runtime forbids it in production).
        import fakeredis

        client: Redis = fakeredis.aioredis.FakeRedis()
        return client
    remote: Redis = Redis.from_url(url, decode_responses=False)
    return remote


def build_infrastructure(
    settings: Settings,
    *,
    redis: Redis | None = None,
    engine: AsyncEngine | None = None,
    resolver: Resolver | None = None,
    clock: Clock | None = None,
) -> Infrastructure:
    settings.validate_for_runtime()
    engine = engine or create_engine(settings.database_url, pool_size=settings.db_pool_size)
    sessionmaker = create_sessionmaker(engine)
    redis = redis or _connect_redis(settings.redis_url)
    llm_factory = OpenAICompatibleProviderFactory(
        timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
    )
    return Infrastructure(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        redis=redis,
        uow_factory=SqlAlchemyUnitOfWorkFactory(sessionmaker),
        system_uow_factory=SystemUnitOfWorkFactory(sessionmaker),
        cipher=AesGcmCipher(KeyRing.from_env(settings.encryption_keys.get_secret_value())),
        llm_factory=llm_factory,
        embedding_factory=EmbeddingProviderFactory(
            http_client=llm_factory.http_client, timeout=settings.llm_timeout_seconds
        ),
        url_guard=SsrfGuard(
            allow_private=settings.llm_allow_private_hosts,
            allow_http=settings.llm_allow_insecure_http,
            resolver=resolver,
        ),
        event_bus=RedisEventBus(redis),
        worker_registry=RedisWorkerRegistry(redis),
        storage=LocalFileStorage(settings.storage_dir),
        # Crawling allows plain http (ports 80/443 only); the LLM guard above requires https.
        crawl_guard=SsrfGuard(
            allow_http=True,
            resolver=resolver,
            test_fixture_hosts=settings.scraper_test_fixture_hosts,
        ),
        clock=clock or SystemClock(),
    )


def llm_deps(infra: Infrastructure) -> LLMDeps:
    return LLMDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        url_guard=infra.url_guard,
        provider_factory=infra.llm_factory,
        embedding_factory=infra.embedding_factory,
        clock=infra.clock,
        rerank_factory=infra.rerank_factory or _RerankFactory(infra),
        vision_factory=infra.vision_factory or _VisionFactory(infra),
    )


class _RerankFactory:
    """Delegates to the shared OpenAI-compatible factory (same HTTP pool + RPM limiters)."""

    def __init__(self, infra: Infrastructure) -> None:
        self._infra = infra

    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> IRerankProvider:
        factory: Any = self._infra.llm_factory
        provider: IRerankProvider = factory.create_rerank(
            base_url=base_url, api_key=api_key, model=model, rpm_limit=rpm_limit
        )
        return provider


class _VisionFactory:
    def __init__(self, infra: Infrastructure) -> None:
        self._infra = infra

    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> IVisionProvider:
        factory: Any = self._infra.llm_factory
        provider: IVisionProvider = factory.create_vision(
            base_url=base_url, api_key=api_key, model=model, rpm_limit=rpm_limit
        )
        return provider


def build_relay(infra: Infrastructure) -> RelayOutbox:
    return RelayOutbox(
        system_uow_factory=infra.system_uow_factory, bus=infra.event_bus, clock=infra.clock
    )


def build_embedding_resolver(infra: Infrastructure) -> GetEmbeddingProvider:
    return GetEmbeddingProvider(llm_deps(infra))


def build_vault(infra: Infrastructure) -> VaultUseCases:
    deps = VaultDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        clock=infra.clock,
        storage=infra.storage,
        pdf=PypdfTextExtractor(),
        stylometry=StylometryAnalyzer(),
        embeddings=build_embedding_resolver(infra),
    )
    return VaultUseCases(
        get_profile=GetProfile(deps),
        save_profile=SaveProfile(deps),
        upload_document=UploadDocument(deps),
        list_documents=ListDocuments(deps),
        document_text=GetDocumentText(deps),
        download_document=DownloadDocument(deps),
        delete_document=DeleteDocument(deps),
        list_essays=ListEssays(deps),
        create_essay=CreateEssay(deps),
        update_essay=UpdateEssay(deps),
        delete_essay=DeleteEssay(deps),
        get_fingerprint=GetFingerprint(deps),
        list_memory=ListMemory(deps),
        learn_answer=LearnAnswer(deps),
        update_memory=UpdateMemory(deps),
        delete_memory=DeleteMemory(deps),
        recall=RecallMemory(deps),
    )


def build_discovery_deps(infra: Infrastructure) -> DiscoveryDeps:
    s = infra.settings
    llm = llm_deps(infra)
    return DiscoveryDeps(
        uow_factory=infra.uow_factory,
        clock=infra.clock,
        url_guard=infra.crawl_guard,
        crawler_factory=SiteCrawlerFactory(
            infra.crawl_guard,
            use_playwright=s.crawl_use_playwright and playwright_available(),
            politeness_seconds=s.crawl_politeness_seconds,
            respect_robots=s.crawl_respect_robots,
        ),
        processor=HtmlContentProcessor(),
        search=build_search_chain(
            tavily_key=s.tavily_api_key.get_secret_value() if s.tavily_api_key else None,
            brave_key=s.brave_api_key.get_secret_value() if s.brave_api_key else None,
            http=infra.llm_factory.http_client,
        ),
        listings=LinkedInPublicAdapter(infra.llm_factory.http_client),
        extractor=LlmOpportunityExtractor(),
        fuzzy=LlmFuzzyEligibilityEvaluator(),
        chat=ResolveChatProvider(llm),
        embeddings=GetEmbeddingProvider(llm),
        campaign_log=FileCampaignLog(s.logs_dir),
        cipher=infra.cipher,
        rerank=ResolveRerankProvider(llm),
        vision=ResolveVisionProvider(llm),
        layout=PdfLayoutParser(PypdfTextExtractor()),
        auto_stage=_auto_stager(infra),
        linkedin_auth=LinkedInAuthenticatedAdapter(infra.crawl_guard)
        if playwright_available()
        else None,
        search_factory=SearchChainFactory(
            infra.llm_factory.http_client,
            default_tavily_key=s.tavily_api_key.get_secret_value() if s.tavily_api_key else None,
            default_brave_key=s.brave_api_key.get_secret_value() if s.brave_api_key else None,
        ),
    )


def _auto_stager(infra: Infrastructure) -> Callable[[uuid.UUID, int], Awaitable[list[uuid.UUID]]]:
    async def stage(user_id: uuid.UUID, limit: int) -> list[uuid.UUID]:
        batch = BatchPrepare(infra.uow_factory, build_application_runner(infra))
        return (await batch.execute(user_id, limit)).started

    return stage


def build_sessions(infra: Infrastructure) -> SessionUseCases:
    s = infra.settings
    # A visible browser only makes sense on the user's own machine (local runtime).
    headful = s.environment != "production" and s.allow_headful_capture and playwright_available()
    deps = SessionDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        clock=infra.clock,
        url_guard=infra.crawl_guard,
        capturer=infra.session_capturer
        or (PlaywrightSessionCapturer(infra.crawl_guard) if headful else None),
        spawn=infra.spawn,
    )
    return SessionUseCases(
        list_sessions=ListBrowserSessions(deps),
        capture=CaptureBrowserSession(deps),
        delete_session=DeleteBrowserSession(deps),
        list_search=ListSearchCredentials(deps),
        save_search=SaveSearchCredential(deps),
        delete_search=DeleteSearchCredential(deps),
        headful_available=deps.capturer is not None,
    )


def build_campaign_runner(infra: Infrastructure) -> ICampaignRunner:
    """In-process by default (dev, memory:// Redis). With CAMPAIGN_RUNNER=arq the API enqueues
    jobs and `uv run worker --queue discovery` executes them."""
    if infra.campaign_runner is None:
        if infra.settings.campaign_runner == "arq":
            from arq.connections import ArqRedis

            from app.infrastructure.runners.campaign_runners import ArqCampaignRunner

            infra.campaign_runner = ArqCampaignRunner(
                ArqRedis(connection_pool=infra.redis.connection_pool)
            )
        else:
            infra.campaign_runner = InProcessCampaignRunner(
                RunCampaign(build_discovery_deps(infra))
            )
    return infra.campaign_runner


def build_discovery(infra: Infrastructure) -> DiscoveryUseCases:
    deps = build_discovery_deps(infra)
    return DiscoveryUseCases(
        create_campaign=CreateCampaign(deps),
        list_campaigns=ListCampaigns(infra.uow_factory),
        get_campaign=GetCampaign(infra.uow_factory),
        cancel_campaign=CancelCampaign(deps),
        list_opportunities=ListOpportunities(infra.uow_factory),
        get_opportunity=GetOpportunity(infra.uow_factory),
        queue=ListApplicationQueue(infra.uow_factory),
        summary=GetPipelineSummary(infra.uow_factory),
        runner=build_campaign_runner(infra),
        retry_page=RetryPage(deps),
        get_page=GetPage(infra.uow_factory),
        log_path=deps.campaign_log.path_for,
        spawn=infra.spawn,
    )


def build_notifiers(infra: Infrastructure) -> dict[NotifChannel, INotifier]:
    s = infra.settings
    http = infra.llm_factory.http_client
    notifiers: dict[NotifChannel, INotifier] = {}
    if s.resend_api_key and s.resend_sender:
        notifiers[NotifChannel.EMAIL] = ResendNotifier(
            s.resend_api_key.get_secret_value(), s.resend_sender, http
        )
    elif s.smtp_host:
        notifiers[NotifChannel.EMAIL] = SmtpNotifier(
            host=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username,
            password=s.smtp_password.get_secret_value() if s.smtp_password else None,
            sender=s.smtp_sender,
            starttls=s.smtp_starttls,
        )
    if s.twilio_account_sid and s.twilio_auth_token and s.twilio_whatsapp_from:
        notifiers[NotifChannel.WHATSAPP] = TwilioWhatsAppNotifier(
            s.twilio_account_sid,
            s.twilio_auth_token.get_secret_value(),
            s.twilio_whatsapp_from,
            http,
            status_callback=f"{s.public_api_url.rstrip('/')}/api/v1/webhooks/twilio",
        )
    elif s.meta_wa_access_token and s.meta_wa_phone_number_id:
        notifiers[NotifChannel.WHATSAPP] = MetaWhatsAppNotifier(
            s.meta_wa_access_token.get_secret_value(),
            s.meta_wa_phone_number_id,
            s.meta_wa_template,
            http,
        )
    if s.notifications_dev_log and not s.is_production:
        for channel in NotifChannel:
            notifiers.setdefault(channel, DevLogNotifier(channel, s.logs_dir))
    return notifiers


def build_notification_deps(infra: Infrastructure) -> NotificationDeps:
    return NotificationDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        clock=infra.clock,
        notifiers=build_notifiers(infra),
        console_url=infra.settings.console_url,
    )


def build_essay_engine(infra: Infrastructure) -> SynthesizeEssay:
    llm = llm_deps(infra)
    return SynthesizeEssay(
        EssayDeps(
            uow_factory=infra.uow_factory,
            clock=infra.clock,
            chat=ResolveChatProvider(llm),
            embeddings=GetEmbeddingProvider(llm),
            stylometry=StylometryAnalyzer(),
        )
    )


def build_application_deps(infra: Infrastructure) -> ApplicationDeps:
    vault = build_vault(infra)
    return ApplicationDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        clock=infra.clock,
        storage=infra.storage,
        filler=PlaywrightFormFiller(infra.crawl_guard),
        get_profile=vault.get_profile,
        download=vault.download_document,
        recall=vault.recall,
        learn=vault.learn_answer,
        update_memory=vault.update_memory,
        essays=build_essay_engine(infra),
        notify=NotifyUser(build_notification_deps(infra)),
        evidence=build_evidence_retriever(infra),
    )


def build_evidence_retriever(infra: Infrastructure) -> ProfileEvidenceRetriever:
    llm = llm_deps(infra)
    vault = VaultDeps(
        uow_factory=infra.uow_factory,
        cipher=infra.cipher,
        clock=infra.clock,
        storage=infra.storage,
        pdf=PypdfTextExtractor(),
        stylometry=StylometryAnalyzer(),
        embeddings=build_embedding_resolver(infra),
    )
    return ProfileEvidenceRetriever(
        EvidenceDeps(
            uow_factory=infra.uow_factory,
            embeddings=GetEmbeddingProvider(llm),
            chat=ResolveChatProvider(llm),
            answerer=LlmEvidenceAnswerer(),
            backfill=IndexMissingDocuments(IndexDocument(vault)),
        )
    )


def build_application_runner(infra: Infrastructure) -> IApplicationRunner:
    if infra.application_runner is None:
        deps = build_application_deps(infra)
        infra.application_runner = InProcessApplicationRunner(
            RunApplication(deps),
            RunSubmission(deps),
            max_parallel=infra.settings.max_parallel_fills,
        )
    return infra.application_runner


def build_maintenance(infra: Infrastructure) -> tuple[ReapLeases, SweepDeadlines]:
    return (
        ReapLeases(infra.system_uow_factory, infra.clock),
        SweepDeadlines(infra.system_uow_factory, infra.clock),
    )


def build_applications(infra: Infrastructure) -> ApplicationUseCases:
    deps = build_application_deps(infra)
    runner = build_application_runner(infra)
    engine = deps.essays
    return ApplicationUseCases(
        detail=GetApplicationDetail(infra.uow_factory, infra.cipher),
        start=StartFilling(infra.uow_factory, infra.clock, runner),
        inbox=GetInbox(infra.uow_factory),
        answer=AnswerQuestion(deps, runner),
        screenshot=GetCheckpointScreenshot(infra.uow_factory, infra.cipher, infra.storage),
        synthesize=engine,
        edit_draft=EditEssayDraft(engine.deps),
        list_drafts=ListEssayDrafts(infra.uow_factory),
        verification=GetVerification(deps),
        batch=BatchPrepare(infra.uow_factory, runner),
        approve=ApproveSubmission(deps, runner.submit),
        reject=RejectApplication(infra.uow_factory, infra.clock),
        receipt=GetReceiptScreenshot(deps),
    )


def build_notifications(infra: Infrastructure) -> NotificationUseCases:
    s = infra.settings
    deps = build_notification_deps(infra)
    return NotificationUseCases(
        save_channel=SaveChannel(deps),
        verify_channel=VerifyChannel(deps),
        list_channels=ListChannels(infra.uow_factory),
        delete_channel=DeleteChannel(infra.uow_factory),
        send_test=SendTestNotification(deps),
        recent=RecentNotifications(infra.uow_factory),
        receipt=RecordDeliveryReceipt(infra.system_uow_factory),
        twilio_auth_token=s.twilio_auth_token.get_secret_value() if s.twilio_auth_token else None,
        meta_app_secret=s.meta_app_secret.get_secret_value() if s.meta_app_secret else None,
        meta_verify_token=s.meta_verify_token.get_secret_value() if s.meta_verify_token else None,
        public_api_url=s.public_api_url,
        providers={c.value: n.name for c, n in deps.notifiers.items()},
    )


def build_sweep(infra: Infrastructure) -> SweepInterruptedCampaigns:
    return SweepInterruptedCampaigns(infra.system_uow_factory, infra.clock)


def build_api_container(infra: Infrastructure) -> ApiContainer:
    s = infra.settings
    auth = AuthDeps(
        uow_factory=infra.uow_factory,
        hasher=Argon2PasswordHasher(
            time_cost=s.argon2_time_cost,
            memory_cost_kib=s.argon2_memory_cost_kib,
            parallelism=s.argon2_parallelism,
        ),
        access_tokens=JwtAccessTokenService(
            secret=s.jwt_secret.get_secret_value(),
            issuer=s.jwt_issuer,
            ttl=timedelta(seconds=s.access_token_ttl_seconds),
        ),
        refresh_codec=RefreshTokenCodec(),
        clock=infra.clock,
        refresh_ttl=timedelta(days=s.refresh_token_ttl_days),
    )
    llm = llm_deps(infra)
    return ApiContainer(
        config=ApiConfig(
            cookie_secure=s.cookie_secure,
            cookie_domain=s.cookie_domain,
            access_ttl_seconds=s.access_token_ttl_seconds,
            refresh_ttl_seconds=s.refresh_token_ttl_days * 86400,
            cors_origins=s.cors_origins,
            sse_heartbeat_seconds=s.sse_heartbeat_seconds,
            expose_docs=not s.is_production,
            dev_auth_bypass=s.dev_auth_bypass and not s.is_production,
        ),
        access_tokens=auth.access_tokens,
        event_bus=infra.event_bus,
        register_user=RegisterUser(auth),
        login_user=LoginUser(auth),
        refresh_session=RefreshSession(auth),
        logout=Logout(auth),
        get_current_user=GetCurrentUser(infra.uow_factory),
        ensure_dev_user=EnsureDevUser(auth),
        create_provider=CreateProviderConfig(llm),
        update_provider=UpdateProviderConfig(llm),
        list_providers=ListProviderConfigs(infra.uow_factory),
        activate_provider=ActivateProviderConfig(llm),
        delete_provider=DeleteProviderConfig(llm),
        verify_connection=VerifyConnection(llm),
        test_prompt=RunTestPrompt(llm),
        fetch_models=FetchModels(llm),
        connection_status=GetConnectionStatus(infra.uow_factory),
        replay_events=ReplayEvents(infra.uow_factory),
        worker_status=GetWorkerStatus(infra.worker_registry),
        vault=build_vault(infra),
        discovery=build_discovery(infra),
        applications=build_applications(infra),
        notifications=build_notifications(infra),
        sessions=build_sessions(infra),
    )
