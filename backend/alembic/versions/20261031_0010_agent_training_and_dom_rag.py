"""Milestone 10: agent trajectories - DOM-RAG memory and imitation-learning data.

* agent_trajectories: one decision on one page per row - the PII-tokenized DOM accessibility
  signature, the chosen action, an optional rejected action (a bot decision a human corrected
  in Teach Mode: a DPO preference pair), and the signature's embedding (vector(1024), HNSW
  cosine) for cross-portal retrieval. RLS like every tenant table.

Revision ID: 0010_agent_training_and_dom_rag
Revises: 0009_agent_brain_and_teach_mode
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0010_agent_training_and_dom_rag"
down_revision: str | None = "0009_agent_brain_and_teach_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_trajectories",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("dom_signature", sa.Text, nullable=False),
        sa.Column("chosen_action", pg.JSONB(), nullable=False),
        sa.Column("rejected_action", pg.JSONB(), nullable=True),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("ALTER TABLE agent_trajectories ADD COLUMN dom_embedding vector(1024) NOT NULL")
    op.create_index(
        "ix_agent_trajectories_user_source", "agent_trajectories", ["user_id", "source"]
    )
    op.create_index(
        "ix_agent_trajectories_user_app", "agent_trajectories", ["user_id", "application_id"]
    )
    op.execute(
        "CREATE INDEX ix_agent_trajectories_embedding_hnsw ON agent_trajectories "
        "USING hnsw (dom_embedding vector_cosine_ops)"
    )
    enable_tenant_rls(op, "agent_trajectories")


def downgrade() -> None:
    disable_tenant_rls(op, "agent_trajectories")
    op.drop_table("agent_trajectories")
