"""Discovery upgrades: bot-challenge status, failed pages never dropped by content hash,
domain-level bot-challenge memory, optional user-provided extraction model, per-user search
API credentials (encrypted), LinkedIn daily action cap on browser sessions.

Revision ID: 0005_discovery_streaming
Revises: 0004_filler_inbox_notify
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0005_discovery_streaming"
down_revision: str | None = "0004_filler_inbox_notify"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE crawl_status ADD VALUE IF NOT EXISTS 'BLOCKED_BOT_CHALLENGE'")

    # Content-hash dedupe only for real, successful content: many failed pages share the same
    # empty/4-char body and must all be kept (they used to vanish via ON CONFLICT DO NOTHING).
    op.drop_constraint("uq_crawled_pages_campaign_sha", "crawled_pages", type_="unique")
    op.create_index(
        "uq_crawled_pages_campaign_content",
        "crawled_pages",
        ["campaign_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text(
            "content_sha256 IS NOT NULL AND status IN ('DISTILLED', 'EXTRACTED')"
        ),
    )

    op.add_column("domain_playbooks", sa.Column("bot_challenge", pg.JSONB, nullable=True))
    op.add_column(
        "llm_provider_configs", sa.Column("extraction_model", sa.String(200), nullable=True)
    )

    # Browser sessions: LinkedIn daily action cap (opt-in authenticated adapter) + bookkeeping.
    op.add_column("browser_sessions", sa.Column("daily_action_cap", sa.Integer, nullable=True))
    op.add_column(
        "browser_sessions",
        sa.Column("actions_today", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("browser_sessions", sa.Column("actions_day", sa.Date, nullable=True))
    op.add_column(
        "browser_sessions",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_browser_sessions_user", "browser_sessions", ["user_id"])

    op.create_table(
        "search_credentials",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("key_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("key_nonce", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.SmallInteger, nullable=False),
        sa.Column("key_last4", sa.String(4), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_search_credentials_user_provider"),
    )
    op.create_index("ix_search_credentials_user", "search_credentials", ["user_id"])
    enable_tenant_rls(op, "search_credentials")


def downgrade() -> None:
    disable_tenant_rls(op, "search_credentials")
    op.drop_table("search_credentials")
    op.drop_index("ix_browser_sessions_user", table_name="browser_sessions")
    for col in ("created_at", "actions_day", "actions_today", "daily_action_cap"):
        op.drop_column("browser_sessions", col)
    op.drop_column("llm_provider_configs", "extraction_model")
    op.drop_column("domain_playbooks", "bot_challenge")
    op.drop_index("uq_crawled_pages_campaign_content", table_name="crawled_pages")
    op.create_unique_constraint(
        "uq_crawled_pages_campaign_sha", "crawled_pages", ["campaign_id", "content_sha256"]
    )
    # crawl_status keeps BLOCKED_BOT_CHALLENGE (Postgres cannot drop an enum value).
