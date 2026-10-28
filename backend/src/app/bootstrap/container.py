"""Composition root: the only place that knows about concrete adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.container import ApiConfig, ApiContainer, DiscoveryUseCases, VaultUseCases
from app.application.auth.use_cases import (
    AuthDeps,
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
    DiscoveryDeps,
    GetCampaign,
    GetOpportunity,
    GetPipelineSummary,
    ListApplicationQueue,
    ListCampaigns,
    ListOpportunities,
    RunCampaign,
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
from app.application.vault_deps import VaultDeps
from app.domain.ports.discovery import ICampaignRunner
from app.domain.shared.clock import Clock, SystemClock
from app.infrastructure.config.settings import Settings
from app.infrastructure.crypto.aes_gcm_cipher import AesGcmCipher
from app.infrastructure.crypto.key_ring import KeyRing
from app.infrastructure.db.session import create_engine, create_sessionmaker
from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory, SystemUnitOfWorkFactory
from app.infrastructure.documents.pdf import PypdfTextExtractor
from app.infrastructure.events.redis_bus import RedisEventBus, RedisWorkerRegistry
from app.infrastructure.extraction.llm_adapters import (
    LlmFuzzyEligibilityEvaluator,
    LlmOpportunityExtractor,
)
from app.infrastructure.linkedin.public_adapter import LinkedInPublicAdapter
from app.infrastructure.llm.embeddings import EmbeddingProviderFactory
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProviderFactory
from app.infrastructure.runners.campaign_runners import InProcessCampaignRunner
from app.infrastructure.scraping.distiller import HtmlContentProcessor
from app.infrastructure.scraping.fetchers import playwright_available
from app.infrastructure.scraping.site_crawler import SiteCrawlerFactory
from app.infrastructure.scraping.ssrf_guard import Resolver, SsrfGuard
from app.infrastructure.search.providers import build_search_chain
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

    async def aclose(self) -> None:
        closer = getattr(self.campaign_runner, "aclose", None)
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
    llm_factory = OpenAICompatibleProviderFactory(timeout=settings.llm_timeout_seconds)
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
    )


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
    )


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
    )
