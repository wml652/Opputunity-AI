"""Milestone 6: in-page RAG + rerank / vision slots, RPM pacing, CV/transcript evidence RAG.

* llm_provider_configs: optional user-typed `rerank_model`, `rerank_base_url`, `vision_model`
  (blank = local fallbacks; never pre-filled) and `rpm_limit` (default 36 requests/minute).
* document_chunks: embedded chunks of the student's uploaded documents (CV, transcript,
  certificates) for evidence retrieval. RLS; vector(1024) + HNSW cosine.
* application_fields.evidence: the (PII-redacted) evidence snippet an answer was grounded in.

Revision ID: 0007_rag_and_batch_apply
Revises: 0006_final_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0007_rag_and_batch_apply"
down_revision: str | None = "0006_final_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_provider_configs", sa.Column("rerank_model", sa.String(200), nullable=True))
    op.add_column("llm_provider_configs", sa.Column("rerank_base_url", sa.Text, nullable=True))
    op.add_column("llm_provider_configs", sa.Column("vision_model", sa.String(200), nullable=True))
    op.add_column(
        "llm_provider_configs",
        sa.Column("rpm_limit", sa.Integer, nullable=False, server_default="36"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1024) NOT NULL")
    op.create_index("ix_document_chunks_user_doc", "document_chunks", ["user_id", "document_id"])
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    enable_tenant_rls(op, "document_chunks")

    op.add_column("application_fields", sa.Column("evidence", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("application_fields", "evidence")
    disable_tenant_rls(op, "document_chunks")
    op.drop_table("document_chunks")
    for col in ("rpm_limit", "vision_model", "rerank_base_url", "rerank_model"):
        op.drop_column("llm_provider_configs", col)
