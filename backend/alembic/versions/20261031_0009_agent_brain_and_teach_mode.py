"""Milestone 9: the agent brain - portal workflows learned in Teach Mode.

* agent_workflows: per user, per portal domain (+ ATS type): the demonstrated navigation path
  to the application form (multi-strategy locators) and the field mappings (profile path or
  semantic-memory id; never personal values, never passwords). RLS like every tenant table.

Revision ID: 0009_agent_brain_and_teach_mode
Revises: 0008_profile_v2_and_ranking
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0009_agent_brain_and_teach_mode"
down_revision: str | None = "0008_profile_v2_and_ranking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_workflows",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("ats_type", sa.String(30), nullable=False),
        sa.Column("navigation_steps", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("field_mappings", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("source_application_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("times_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "domain", "ats_type", name="uq_agent_workflows_domain"),
    )
    op.create_index("ix_agent_workflows_user_ats", "agent_workflows", ["user_id", "ats_type"])
    enable_tenant_rls(op, "agent_workflows")


def downgrade() -> None:
    disable_tenant_rls(op, "agent_workflows")
    op.drop_table("agent_workflows")
