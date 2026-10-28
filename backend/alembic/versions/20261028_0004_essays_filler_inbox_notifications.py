"""Flight-recorder traces, domain playbooks, essay drafts, form filler, Action Inbox, notifications.

Revision ID: 0004_filler_inbox_notify
Revises: 0003_discovery_and_eligibility
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0004_filler_inbox_notify"
down_revision: str | None = "0003_discovery_and_eligibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (
    "domain_playbooks",
    "application_checkpoints",
    "application_fields",
    "pending_questions",
    "essay_drafts",
    "notification_channels",
    "notifications",
    "browser_sessions",
)


def _uuid() -> pg.UUID:  # type: ignore[type-arg]
    return pg.UUID(as_uuid=True)


def _owner() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "user_id", _uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _fk(name: str, table: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name, _uuid(), sa.ForeignKey(f"{table}.id", ondelete=ondelete), nullable=nullable
    )


def _ts(name: str, *, nullable: bool = False, default_now: bool = True) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


def _enum(name: str) -> pg.ENUM:
    return pg.ENUM(name=name, create_type=False)


def _secret(prefix: str, *, nullable: bool = False) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(f"{prefix}_ciphertext", sa.LargeBinary, nullable=nullable),
        sa.Column(f"{prefix}_nonce", sa.LargeBinary, nullable=nullable),
        sa.Column(f"{prefix}_key_version", sa.SmallInteger, nullable=nullable),
    ]


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE must commit before the value can be used.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE crawl_status ADD VALUE IF NOT EXISTS 'SKIPPED'")

    # ---- flight recorder ------------------------------------------------------------------------
    op.add_column(
        "crawled_pages", sa.Column("trace", pg.JSONB, nullable=False, server_default="[]")
    )
    op.add_column("crawled_pages", sa.Column("error_stage", sa.String(40), nullable=True))
    op.add_column("crawled_pages", sa.Column("error_code", sa.String(40), nullable=True))
    op.add_column("crawled_pages", sa.Column("error_detail", sa.Text, nullable=True))
    op.add_column("crawled_pages", sa.Column("discovery_source", sa.String(30), nullable=True))
    op.add_column("crawled_pages", sa.Column("discovery_query", sa.Text, nullable=True))
    op.add_column(
        "crawled_pages", sa.Column("strategy", pg.JSONB, nullable=False, server_default="{}")
    )

    op.create_table(
        "domain_playbooks",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("fetch_strategy", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("extraction_strategy", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("failures", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("dead_urls", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("successes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_campaign_id", _uuid(), nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("user_id", "domain", name="uq_domain_playbooks_user_domain"),
    )

    # ---- applications ---------------------------------------------------------------------------
    op.create_table(
        "application_checkpoints",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("application_id", "applications"),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("step_index", sa.Integer, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("dom_fingerprint", sa.String(64), nullable=False),
        *_secret("storage_state"),
        sa.Column("fill_ledger", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("screenshot_key", sa.Text, nullable=True),
        sa.Column("screenshot_nonce", sa.LargeBinary, nullable=True),
        sa.Column("screenshot_key_version", sa.SmallInteger, nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("application_id", "seq", name="uq_application_checkpoints_seq"),
    )
    op.create_index(
        "ix_application_checkpoints_app", "application_checkpoints", ["user_id", "application_id"]
    )

    op.create_table(
        "application_fields",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("application_id", "applications"),
        sa.Column("step_index", sa.Integer, nullable=False),
        sa.Column("field_key", sa.String(200), nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("field_type", sa.String(20), nullable=False),
        sa.Column("options", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.false()),
        *_secret("value", nullable=True),
        sa.Column("value_source", sa.String(12), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("memory_qa_id", _uuid(), nullable=True),
        sa.Column("document_id", _uuid(), nullable=True),
        sa.Column("essay_draft_id", _uuid(), nullable=True),
        sa.Column("user_edited", sa.Boolean, nullable=False, server_default=sa.false()),
        _ts("updated_at"),
        sa.UniqueConstraint("application_id", "field_key", name="uq_application_fields_key"),
    )
    op.create_index(
        "ix_application_fields_app", "application_fields", ["user_id", "application_id"]
    )

    op.create_table(
        "pending_questions",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("application_id", "applications"),
        sa.Column("field_key", sa.String(200), nullable=False),
        sa.Column("step_index", sa.Integer, nullable=False),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("context", sa.Text, nullable=False, server_default=""),
        sa.Column("field_type", sa.String(20), nullable=False),
        sa.Column("options", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("suggested_answer", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("status", _enum("question_status"), nullable=False),
        sa.Column("answered_qa_id", _uuid(), nullable=True),
        _ts("created_at"),
        _ts("answered_at", nullable=True, default_now=False),
    )
    op.create_index(
        "ix_pending_questions_user_status", "pending_questions", ["user_id", "status", "created_at"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pending_questions_open ON pending_questions "
        "(application_id, field_key) WHERE status = 'OPEN'"
    )

    op.create_table(
        "essay_drafts",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("application_id", "applications", nullable=True),
        _fk("opportunity_id", "opportunities", nullable=True, ondelete="SET NULL"),
        sa.Column("field_key", sa.String(200), nullable=True),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("word_limit", sa.Integer, nullable=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("critique", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("voice_match_score", sa.Float, nullable=True),
        sa.Column("burstiness_score", sa.Float, nullable=True),
        sa.Column("status", _enum("essay_status"), nullable=False),
        sa.Column("requires_manual", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("fingerprint_version", sa.Integer, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_essay_drafts_user_app", "essay_drafts", ["user_id", "application_id", "created_at"]
    )

    # ---- notifications --------------------------------------------------------------------------
    op.create_table(
        "notification_channels",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("channel", _enum("notif_channel"), nullable=False),
        *_secret("address"),
        sa.Column("address_hint", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("verified_at", nullable=True, default_now=False),
        sa.Column("verify_code_hash", sa.String(64), nullable=True),
        _ts("verify_expires_at", nullable=True, default_now=False),
        _ts("created_at"),
        sa.UniqueConstraint("user_id", "channel", name="uq_notification_channels_user_channel"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("channel", _enum("notif_channel"), nullable=False),
        sa.Column("template", sa.String(30), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", _enum("notif_status"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        _fk("related_application_id", "applications", nullable=True, ondelete="SET NULL"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("user_id", "channel", "dedupe_key", name="uq_notifications_dedupe"),
    )
    op.create_index("ix_notifications_provider_id", "notifications", ["provider_message_id"])

    op.create_table(
        "browser_sessions",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        *_secret("storage_state"),
        _ts("tos_acknowledged_at", nullable=True, default_now=False),
        _ts("last_used_at", nullable=True, default_now=False),
        _ts("expires_at", nullable=True, default_now=False),
        sa.UniqueConstraint(
            "user_id", "domain", "kind", name="uq_browser_sessions_user_domain_kind"
        ),
    )

    for table in NEW_TABLES:
        enable_tenant_rls(op, table)


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        disable_tenant_rls(op, table)
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    for column in (
        "strategy",
        "discovery_query",
        "discovery_source",
        "error_detail",
        "error_code",
        "error_stage",
        "trace",
    ):
        op.drop_column("crawled_pages", column)
