"""Discovery campaigns, crawled pages, opportunities (vector), eligibility, application queue.

Revision ID: 0003_discovery_and_eligibility
Revises: 0002_student_vault_and_memory
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0003_discovery_and_eligibility"
down_revision: str | None = "0002_student_vault_and_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "campaigns",
    "campaign_sources",
    "crawled_pages",
    "opportunities",
    "eligibility_verdicts",
    "applications",
    "application_transitions",
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


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scope", _enum("scope"), nullable=False),
        sa.Column("keywords", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("status", _enum("campaign_status"), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("settings", pg.JSONB, nullable=False),
        sa.Column("counters", pg.JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("started_at", nullable=True, default_now=False),
        _ts("finished_at", nullable=True, default_now=False),
    )
    op.create_index("ix_campaigns_user_created", "campaigns", ["user_id", "created_at"])

    op.create_table(
        "campaign_sources",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("campaign_id", "campaigns"),
        sa.Column("source_type", _enum("source_type"), nullable=False),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_campaign_sources_campaign", "campaign_sources", ["user_id", "campaign_id"])

    op.create_table(
        "crawled_pages",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("campaign_id", "campaigns"),
        _fk("source_id", "campaign_sources", nullable=True, ondelete="SET NULL"),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text, nullable=True),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("status", _enum("crawl_status"), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("markdown", sa.Text, nullable=True),
        sa.Column("injection_flags", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("error", sa.Text, nullable=True),
        _ts("fetched_at"),
        # NULL hashes (blocked/failed pages) never collide: NULLs are distinct in UNIQUE.
        sa.UniqueConstraint("campaign_id", "content_sha256", name="uq_crawled_pages_campaign_sha"),
    )
    op.create_index(
        "ix_crawled_pages_campaign_status", "crawled_pages", ["user_id", "campaign_id", "status"]
    )

    op.create_table(
        "opportunities",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("campaign_id", "campaigns", nullable=True, ondelete="SET NULL"),
        _fk("page_id", "crawled_pages", nullable=True, ondelete="SET NULL"),
        sa.Column("type", _enum("opportunity_type"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("organization", sa.String(200), nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("is_international", sa.Boolean, nullable=True),
        _ts("deadline_at", nullable=True, default_now=False),
        sa.Column("deadline_raw", sa.String(200), nullable=True),
        sa.Column("portal_url", sa.Text, nullable=True),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("eligibility_rules", pg.JSONB, nullable=False),
        sa.Column("ai_policy", sa.String(12), nullable=False, server_default="UNKNOWN"),
        sa.Column("dedupe_key", sa.String(500), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("latest_outcome", _enum("eligibility_outcome"), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_opportunities_user_dedupe"),
        sa.CheckConstraint(
            "ai_policy IN ('ALLOWED','PROHIBITED','UNKNOWN')", name="ck_opportunities_ai_policy"
        ),
    )
    op.execute("ALTER TABLE opportunities ADD COLUMN embedding vector(1024) NOT NULL")
    op.create_index(
        "ix_opportunities_user_outcome_deadline",
        "opportunities",
        ["user_id", "latest_outcome", "deadline_at"],
    )
    op.execute(
        "CREATE INDEX ix_opportunities_embedding_hnsw ON opportunities "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "eligibility_verdicts",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("opportunity_id", "opportunities"),
        sa.Column("outcome", _enum("eligibility_outcome"), nullable=False),
        sa.Column("reasons", pg.JSONB, nullable=False),
        sa.Column("profile_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        _ts("evaluated_at"),
    )
    op.create_index(
        "ix_eligibility_verdicts_opp",
        "eligibility_verdicts",
        ["user_id", "opportunity_id", "evaluated_at"],
    )

    op.create_table(
        "applications",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("opportunity_id", "opportunities"),
        sa.Column("state", _enum("application_state"), nullable=False),
        sa.Column("priority_rank", sa.Integer, nullable=True),
        _ts("deadline_at", nullable=True, default_now=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lock_owner", sa.String(120), nullable=True),
        _ts("lease_expires_at", nullable=True, default_now=False),
        sa.Column("last_error", sa.Text, nullable=True),
        _ts("submitted_at", nullable=True, default_now=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("user_id", "opportunity_id", name="uq_applications_user_opportunity"),
    )
    op.create_index(
        "ix_applications_user_state_deadline", "applications", ["user_id", "state", "deadline_at"]
    )

    op.create_table(
        "application_transitions",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        _fk("application_id", "applications"),
        sa.Column("from_state", _enum("application_state"), nullable=True),
        sa.Column("to_state", _enum("application_state"), nullable=False),
        sa.Column("actor", _enum("actor"), nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("metadata", pg.JSONB, nullable=False, server_default="{}"),
        _ts("at"),
    )
    op.create_index(
        "ix_application_transitions_app",
        "application_transitions",
        ["user_id", "application_id", "at"],
    )

    for table in TABLES:
        enable_tenant_rls(op, table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_tenant_rls(op, table)
    for table in reversed(TABLES):
        op.drop_table(table)
