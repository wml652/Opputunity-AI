"""SQLAlchemy ORM rows (persistence model). Mapped to/from domain dataclasses in repositories."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.llm.provider import ProviderKind
from app.domain.memory.models import MemorySource
from app.domain.profile.models import DegreeLevel
from app.domain.shared.enums import (
    Actor,
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    DocKind,
    EligibilityOutcome,
    EssayStatus,
    NotifChannel,
    NotifStatus,
    OpportunityType,
    QuestionStatus,
    Scope,
    SourceType,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_cls: type[Any], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        create_type=False,  # types are created by the baseline migration
        values_callable=lambda e: [m.value for m in e],
    )


def _ts(*, nullable: bool = False, default_now: bool = False) -> Mapped[Any]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (Index("uq_users_email_lower", func.lower(email), unique=True),)


class RefreshTokenRow(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (Index("ix_refresh_tokens_user_family", "user_id", "family_id"),)


class LLMProviderConfigRow(Base):
    __tablename__ = "llm_provider_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ProviderKind] = mapped_column(_pg_enum(ProviderKind, "provider_kind"))
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    embedding_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Optional, user-typed fast model for bulk discovery extraction (blank = use `model`).
    extraction_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rerank_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rerank_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    vision_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rpm_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="36")
    embedding_api_key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_api_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        Index("ix_llm_provider_configs_user", "user_id"),
        Index(
            "uq_llm_provider_configs_active",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class LLMConnectionCheckRow(Base):
    __tablename__ = "llm_connection_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_listed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        Index(
            "ix_llm_connection_checks_user_config", "user_id", "provider_config_id", "checked_at"
        ),
    )


class PipelineEventRow(Base):
    __tablename__ = "pipeline_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _ts(default_now=True)
    published_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        Index("ix_pipeline_events_user_seq", "user_id", "seq"),
        Index(
            "ix_pipeline_events_unpublished",
            "seq",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[Actor] = mapped_column(_pg_enum(Actor, "actor"))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (Index("ix_audit_log_user_at", "user_id", "at"),)


def _owner() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class StudentProfileRow(Base):
    __tablename__ = "student_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pii_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pii_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pii_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    degree_level: Mapped[DegreeLevel | None] = mapped_column(
        _pg_enum(DegreeLevel, "degree_level"), nullable=True
    )
    major: Mapped[str | None] = mapped_column(String(200), nullable=True)
    institution: Mapped[str | None] = mapped_column(String(200), nullable=True)
    gpa: Mapped[Any] = mapped_column(Numeric(6, 2), nullable=True)
    gpa_scale: Mapped[Any] = mapped_column(Numeric(6, 2), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country_of_residence: Mapped[str | None] = mapped_column(String(100), nullable=True)
    graduation_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skills: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    languages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    experiences: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Profile v2 non-PII details (ProfileDetails.to_dict()).
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (UniqueConstraint("user_id", name="uq_student_profiles_user"),)


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    kind: Mapped[DocKind] = mapped_column(_pg_enum(DocKind, "doc_kind"))
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    blob_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    blob_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    text_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    text_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    text_chars: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        Index("ix_documents_user_created", "user_id", "created_at"),
        UniqueConstraint("user_id", "sha256", name="uq_documents_user_sha256"),
    )


class ReferenceEssayRow(Base):
    __tablename__ = "reference_essays"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (Index("ix_reference_essays_user", "user_id", "created_at"),)


class StyleFingerprintRow(Base):
    __tablename__ = "style_fingerprints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    essay_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_words: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    feature_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    feature_vector: Mapped[list[float]] = mapped_column(ARRAY(REAL), nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_style_fingerprints_ver"),)


# `essay_chunks.embedding` and `knowledge_memory_qa.embedding` are vector(1024) columns written
# and searched with explicit SQL (CAST(text AS vector)); they are deliberately not ORM-mapped so
# vectors are never loaded by ordinary queries.
class KnowledgeMemoryRow(Base):
    __tablename__ = "knowledge_memory_qa"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[MemorySource] = mapped_column(_pg_enum(MemorySource, "memory_source"))
    times_reused: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


class CampaignRow(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    scope: Mapped[Scope] = mapped_column(_pg_enum(Scope, "scope"))
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(_pg_enum(CampaignStatus, "campaign_status"))
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    counters: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)


class CampaignSourceRow(Base):
    __tablename__ = "campaign_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[SourceType] = mapped_column(_pg_enum(SourceType, "source_type"))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class CrawledPageRow(Base):
    __tablename__ = "crawled_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_sources.id", ondelete="SET NULL"), nullable=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[CrawlStatus] = mapped_column(_pg_enum(CrawlStatus, "crawl_status"))
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    injection_flags: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = _ts(default_now=True)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    error_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    discovery_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        Index(
            "uq_crawled_pages_campaign_content",
            "campaign_id",
            "content_sha256",
            unique=True,
            postgresql_where=text(
                "content_sha256 IS NOT NULL AND status IN ('DISTILLED', 'EXTRACTED')"
            ),
        ),
    )


# `opportunities.embedding` is vector(1024), written/searched via explicit SQL like memory.
class OpportunityRow(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crawled_pages.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[OpportunityType] = mapped_column(_pg_enum(OpportunityType, "opportunity_type"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_international: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    deadline_at: Mapped[datetime | None] = _ts(nullable=True)
    deadline_raw: Mapped[str | None] = mapped_column(String(200), nullable=True)
    portal_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    eligibility_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ai_policy: Mapped[str] = mapped_column(String(12), nullable=False, server_default="UNKNOWN")
    dedupe_key: Mapped[str] = mapped_column(String(500), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    latest_outcome: Mapped[EligibilityOutcome | None] = mapped_column(
        _pg_enum(EligibilityOutcome, "eligibility_outcome"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


class EligibilityVerdictRow(Base):
    __tablename__ = "eligibility_verdicts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    outcome: Mapped[EligibilityOutcome] = mapped_column(
        _pg_enum(EligibilityOutcome, "eligibility_outcome")
    )
    reasons: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    profile_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evaluated_at: Mapped[datetime] = _ts(default_now=True)


class ApplicationRow(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[ApplicationState] = mapped_column(_pg_enum(ApplicationState, "application_state"))
    priority_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[datetime | None] = _ts(nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lock_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = _ts(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = _ts(nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


class ApplicationTransitionRow(Base):
    __tablename__ = "application_transitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[ApplicationState | None] = mapped_column(
        _pg_enum(ApplicationState, "application_state"), nullable=True
    )
    to_state: Mapped[ApplicationState] = mapped_column(
        _pg_enum(ApplicationState, "application_state")
    )
    actor: Mapped[Actor] = mapped_column(_pg_enum(Actor, "actor"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    at: Mapped[datetime] = _ts(default_now=True)


class DomainPlaybookRow(Base):
    __tablename__ = "domain_playbooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    fetch_strategy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    extraction_strategy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    failures: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, server_default="{}")
    dead_urls: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    successes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)
    bot_challenge: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class SearchCredentialRow(Base):
    __tablename__ = "search_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


class BrowserSessionRow(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_state_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_state_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_state_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    tos_acknowledged_at: Mapped[datetime | None] = _ts(nullable=True)
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    daily_action_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actions_today: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    actions_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)


class AgentWorkflowRow(Base):
    """A portal workflow learned in Teach Mode (see domain.agent)."""

    __tablename__ = "agent_workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    ats_type: Mapped[str] = mapped_column(String(30), nullable=False)
    navigation_steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    field_mappings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    source_application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("user_id", "domain", "ats_type", name="uq_agent_workflows_domain"),
    )


class FinalVerificationRow(Base):
    __tablename__ = "final_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_checkpoints.id", ondelete="SET NULL"),
        nullable=True,
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    preview_hash_seen: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_preview_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    edited_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    mismatches: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    approved_at: Mapped[datetime | None] = _ts(nullable=True)
    rejected_at: Mapped[datetime | None] = _ts(nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = _ts(nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    receipt_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    receipt_text_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    receipt_text_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    receipt_text_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)


class ApplicationCheckpointRow(Base):
    __tablename__ = "application_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    dom_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_state_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_state_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_state_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fill_ledger: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    screenshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    screenshot_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)


class ApplicationFieldRow(Base):
    __tablename__ = "application_fields"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    field_key: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    value_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    value_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    value_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    value_source: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    memory_qa_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    essay_draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = _ts(default_now=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("application_id", "field_key", name="uq_application_fields_key"),
    )


class PendingQuestionRow(Base):
    __tablename__ = "pending_questions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(200), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    suggested_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    status: Mapped[QuestionStatus] = mapped_column(_pg_enum(QuestionStatus, "question_status"))
    answered_qa_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    answered_at: Mapped[datetime | None] = _ts(nullable=True)


class EssayDraftRow(Base):
    __tablename__ = "essay_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=True
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True
    )
    field_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    word_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    critique: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    voice_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    burstiness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[EssayStatus] = mapped_column(_pg_enum(EssayStatus, "essay_status"))
    requires_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fingerprint_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


class NotificationChannelRow(Base):
    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    channel: Mapped[NotifChannel] = mapped_column(_pg_enum(NotifChannel, "notif_channel"))
    address_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    address_hint: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    verified_at: Mapped[datetime | None] = _ts(nullable=True)
    verify_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_expires_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = _owner()
    channel: Mapped[NotifChannel] = mapped_column(_pg_enum(NotifChannel, "notif_channel"))
    template: Mapped[str] = mapped_column(String(30), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[NotifStatus] = mapped_column(_pg_enum(NotifStatus, "notif_status"))
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)


TENANT_TABLES = (
    "refresh_tokens",
    "llm_provider_configs",
    "llm_connection_checks",
    "pipeline_events",
    "audit_log",
    "student_profiles",
    "documents",
    "reference_essays",
    "essay_chunks",
    "document_chunks",
    "style_fingerprints",
    "knowledge_memory_qa",
    "campaigns",
    "campaign_sources",
    "crawled_pages",
    "opportunities",
    "eligibility_verdicts",
    "applications",
    "application_transitions",
    "domain_playbooks",
    "application_checkpoints",
    "application_fields",
    "pending_questions",
    "essay_drafts",
    "notification_channels",
    "notifications",
    "browser_sessions",
    "search_credentials",
    "final_verifications",
    "agent_workflows",
    "agent_trajectories",
)
