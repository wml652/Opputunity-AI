"""Student vault, stylometry, and semantic Q&A memory (pgvector).

Revision ID: 0002_student_vault_and_memory
Revises: 0001_baseline
Create Date: 2026-10-28

Vector columns are vector(1024) with HNSW cosine indexes (supported since pgvector 0.5; this
targets 0.6.2+). Every table is tenant-scoped with forced row-level security.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.domain.memory.models import MemorySource
from app.domain.profile.models import DegreeLevel
from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0002_student_vault_and_memory"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "student_profiles",
    "documents",
    "reference_essays",
    "essay_chunks",
    "style_fingerprints",
    "knowledge_memory_qa",
)


def _uuid() -> pg.UUID:  # type: ignore[type-arg]
    return pg.UUID(as_uuid=True)


def _owner() -> sa.Column:  # type: ignore[type-arg]
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


def _enum(name: str) -> pg.ENUM:
    return pg.ENUM(name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    pg.ENUM(*[m.value for m in DegreeLevel], name="degree_level").create(bind, checkfirst=True)
    pg.ENUM(*[m.value for m in MemorySource], name="memory_source").create(bind, checkfirst=True)

    op.create_table(
        "student_profiles",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("pii_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("pii_nonce", sa.LargeBinary, nullable=True),
        sa.Column("pii_key_version", sa.SmallInteger, nullable=True),
        sa.Column("degree_level", _enum("degree_level"), nullable=True),
        sa.Column("major", sa.String(200), nullable=True),
        sa.Column("institution", sa.String(200), nullable=True),
        sa.Column("gpa", sa.Numeric(6, 2), nullable=True),
        sa.Column("gpa_scale", sa.Numeric(6, 2), nullable=True),
        sa.Column("nationality", sa.String(100), nullable=True),
        sa.Column("country_of_residence", sa.String(100), nullable=True),
        sa.Column("graduation_year", sa.Integer, nullable=True),
        sa.Column("skills", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("languages", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("experiences", pg.JSONB, nullable=False, server_default="[]"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("user_id", name="uq_student_profiles_user"),
        sa.CheckConstraint(
            "gpa IS NULL OR (gpa_scale > 0 AND gpa >= 0 AND gpa <= gpa_scale)",
            name="ck_student_profiles_gpa_range",
        ),
    )

    op.create_table(
        "documents",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("kind", _enum("doc_kind"), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("blob_nonce", sa.LargeBinary, nullable=False),
        sa.Column("blob_key_version", sa.SmallInteger, nullable=False),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("text_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("text_nonce", sa.LargeBinary, nullable=True),
        sa.Column("text_key_version", sa.SmallInteger, nullable=True),
        sa.Column("text_chars", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_documents_user_sha256"),
    )
    op.create_index("ix_documents_user_created", "documents", ["user_id", "created_at"])

    op.create_table(
        "reference_essays",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("prompt_context", sa.Text, nullable=True),
        sa.Column("word_count", sa.Integer, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_reference_essays_user", "reference_essays", ["user_id", "created_at"])

    op.create_table(
        "essay_chunks",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column(
            "essay_id",
            _uuid(),
            sa.ForeignKey("reference_essays.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        _ts("created_at"),
    )
    op.execute("ALTER TABLE essay_chunks ADD COLUMN embedding vector(1024) NOT NULL")
    op.create_index("ix_essay_chunks_user_essay", "essay_chunks", ["user_id", "essay_id"])
    op.execute(
        "CREATE INDEX ix_essay_chunks_embedding_hnsw ON essay_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "style_fingerprints",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("essay_count", sa.Integer, nullable=False),
        sa.Column("total_words", sa.Integer, nullable=False),
        sa.Column("metrics", pg.JSONB, nullable=False),
        sa.Column("feature_names", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("feature_vector", pg.ARRAY(pg.REAL), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("user_id", "version", name="uq_style_fingerprints_ver"),
    )

    op.create_table(
        "knowledge_memory_qa",
        sa.Column("id", _uuid(), primary_key=True),
        _owner(),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("question_normalized", sa.Text, nullable=False),
        sa.Column("answer_text", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("source", _enum("memory_source"), nullable=False),
        sa.Column("times_reused", sa.Integer, nullable=False, server_default="0"),
        _ts("last_used_at", nullable=True, default_now=False),
        sa.Column("opportunity_id", _uuid(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint(
            "user_id", "question_normalized", name="uq_knowledge_memory_qa_question"
        ),
    )
    op.execute("ALTER TABLE knowledge_memory_qa ADD COLUMN embedding vector(1024) NOT NULL")
    op.create_index(
        "ix_knowledge_memory_qa_user_model", "knowledge_memory_qa", ["user_id", "embedding_model"]
    )
    op.execute(
        "CREATE INDEX ix_knowledge_memory_qa_embedding_hnsw ON knowledge_memory_qa "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    for table in TABLES:
        enable_tenant_rls(op, table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_tenant_rls(op, table)
    for table in (
        "knowledge_memory_qa",
        "style_fingerprints",
        "essay_chunks",
        "reference_essays",
        "documents",
        "student_profiles",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    pg.ENUM(name="memory_source").drop(bind, checkfirst=True)
    pg.ENUM(name="degree_level").drop(bind, checkfirst=True)
