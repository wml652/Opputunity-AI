"""Baseline: all persisted enums, RLS helpers, identity, LLM gateway, outbox, audit.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.domain.llm.provider import ProviderKind
from app.domain.shared import enums
from app.infrastructure.db.rls import (
    DROP_HELPER_FUNCTIONS_SQL,
    HELPER_FUNCTIONS_SQL,
    disable_tenant_rls,
    enable_tenant_rls,
)

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_TYPES: dict[str, type] = {
    "provider_kind": ProviderKind,
    "scope": enums.Scope,
    "source_type": enums.SourceType,
    "campaign_status": enums.CampaignStatus,
    "crawl_status": enums.CrawlStatus,
    "opportunity_type": enums.OpportunityType,
    "eligibility_outcome": enums.EligibilityOutcome,
    "application_state": enums.ApplicationState,
    "question_status": enums.QuestionStatus,
    "doc_kind": enums.DocKind,
    "notif_channel": enums.NotifChannel,
    "notif_status": enums.NotifStatus,
    "essay_status": enums.EssayStatus,
    "actor": enums.Actor,
}

TENANT_TABLES = (
    "refresh_tokens",
    "llm_provider_configs",
    "llm_connection_checks",
    "pipeline_events",
    "audit_log",
)


def _enum(name: str) -> pg.ENUM:
    return pg.ENUM(name=name, create_type=False)


def _uuid() -> pg.UUID:  # type: ignore[type-arg]
    return pg.UUID(as_uuid=True)


def _owner_fk() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "user_id", _uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _ts(name: str, *, nullable: bool = False, default_now: bool = True) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name, enum_cls in ENUM_TYPES.items():
        pg.ENUM(*[m.value for m in enum_cls], name=name).create(bind, checkfirst=True)  # type: ignore[attr-defined]
    for statement in HELPER_FUNCTIONS_SQL:
        op.execute(statement)

    op.create_table(
        "users",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", _uuid(), primary_key=True),
        _owner_fk(),
        sa.Column("family_id", _uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        _ts("expires_at", default_now=False),
        _ts("revoked_at", nullable=True, default_now=False),
        sa.Column("replaced_by", _uuid(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_refresh_tokens_user_family", "refresh_tokens", ["user_id", "family_id"])

    op.create_table(
        "llm_provider_configs",
        sa.Column("id", _uuid(), primary_key=True),
        _owner_fk(),
        sa.Column("kind", _enum("provider_kind"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_key_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("api_key_nonce", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.SmallInteger, nullable=False),
        sa.Column("api_key_last4", sa.String(4), nullable=False),
        sa.Column("embedding_base_url", sa.Text, nullable=True),
        sa.Column("embedding_model", sa.String(200), nullable=True),
        sa.Column("embedding_api_key_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("embedding_api_key_nonce", sa.LargeBinary, nullable=True),
        sa.Column("embedding_key_version", sa.SmallInteger, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.false()),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_llm_provider_configs_user", "llm_provider_configs", ["user_id"])
    op.create_index(
        "uq_llm_provider_configs_active",
        "llm_provider_configs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "llm_connection_checks",
        sa.Column("id", _uuid(), primary_key=True),
        _owner_fk(),
        sa.Column(
            "provider_config_id",
            _uuid(),
            sa.ForeignKey("llm_provider_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("model_listed", sa.Boolean, nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        _ts("checked_at"),
    )
    op.create_index(
        "ix_llm_connection_checks_user_config",
        "llm_connection_checks",
        ["user_id", "provider_config_id", "checked_at"],
    )

    op.create_table(
        "pipeline_events",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        _owner_fk(),
        sa.Column("campaign_id", _uuid(), nullable=True),
        sa.Column("application_id", _uuid(), nullable=True),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False, server_default="{}"),
        _ts("created_at"),
        _ts("published_at", nullable=True, default_now=False),
    )
    op.create_index("ix_pipeline_events_user_seq", "pipeline_events", ["user_id", "seq"])
    op.create_index(
        "ix_pipeline_events_unpublished",
        "pipeline_events",
        ["seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    # Wake the outbox relay immediately on commit (it falls back to polling without this).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_pipeline_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('pipeline_events', NEW.seq::text);
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER pipeline_events_notify AFTER INSERT ON pipeline_events
        FOR EACH ROW EXECUTE FUNCTION notify_pipeline_event()
        """
    )

    op.create_table(
        "audit_log",
        sa.Column("id", _uuid(), primary_key=True),
        _owner_fk(),
        sa.Column("actor", _enum("actor"), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity", sa.String(80), nullable=False),
        sa.Column("entity_id", _uuid(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("metadata", pg.JSONB, nullable=False, server_default="{}"),
        _ts("at"),
    )
    op.create_index("ix_audit_log_user_at", "audit_log", ["user_id", "at"])

    for table in TENANT_TABLES:
        enable_tenant_rls(op, table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        disable_tenant_rls(op, table)
    op.drop_table("audit_log")
    op.execute("DROP TRIGGER IF EXISTS pipeline_events_notify ON pipeline_events")
    op.execute("DROP FUNCTION IF EXISTS notify_pipeline_event()")
    op.drop_table("pipeline_events")
    op.drop_table("llm_connection_checks")
    op.drop_table("llm_provider_configs")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    for statement in DROP_HELPER_FUNCTIONS_SQL:
        op.execute(statement)
    bind = op.get_bind()
    for name in reversed(list(ENUM_TYPES)):
        pg.ENUM(name=name).drop(bind, checkfirst=True)
