from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.essay.models import ReferenceEssay, StyleFingerprint
from app.domain.llm.embedding import ensure_dim
from app.domain.llm.provider import EncryptedSecret
from app.domain.memory.models import KnowledgeQA
from app.domain.ports.vault import (
    DocumentHit,
    EmbeddedChunk,
    ScoredQA,
    StoredDocument,
    StoredProfile,
)
from app.domain.profile.documents import Document
from app.domain.profile.models import (
    GPA,
    Experience,
    Language,
    ProfileDetails,
    StudentPII,
    StudentProfile,
)
from app.domain.shared.ids import new_uuid7
from app.infrastructure.db.models import (
    DocumentRow,
    KnowledgeMemoryRow,
    ReferenceEssayRow,
    StudentProfileRow,
    StyleFingerprintRow,
)


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text format; validated to exactly 1024 finite dims before it reaches SQL."""
    (checked,) = ensure_dim([vector], "storage")
    return "[" + ",".join(repr(float(x)) for x in checked) + "]"


# ---- profiles ----------------------------------------------------------------------------------


class SqlProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, user_id: uuid.UUID) -> StoredProfile | None:
        row = await self._s.scalar(
            select(StudentProfileRow).where(StudentProfileRow.user_id == user_id)
        )
        if row is None:
            return None
        gpa = (
            GPA(Decimal(row.gpa), Decimal(row.gpa_scale))
            if row.gpa is not None and row.gpa_scale is not None
            else None
        )
        profile = StudentProfile(
            id=row.id,
            user_id=row.user_id,
            full_name=row.full_name,
            pii=StudentPII(),
            degree_level=row.degree_level,
            major=row.major,
            institution=row.institution,
            gpa=gpa,
            nationality=row.nationality,
            country_of_residence=row.country_of_residence,
            graduation_year=row.graduation_year,
            skills=list(row.skills or []),
            languages=[Language(**lang) for lang in row.languages or []],
            experiences=[Experience(**exp) for exp in row.experiences or []],
            details=ProfileDetails.from_dict(row.details),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        secret = None
        if row.pii_ciphertext and row.pii_nonce and row.pii_key_version is not None:
            secret = EncryptedSecret(
                ciphertext=row.pii_ciphertext, nonce=row.pii_nonce, key_version=row.pii_key_version
            )
        return StoredProfile(profile=profile, pii=secret)

    async def save(self, profile: StudentProfile, pii: EncryptedSecret | None) -> None:
        values: dict[str, Any] = {
            "full_name": profile.full_name,
            "pii_ciphertext": pii.ciphertext if pii else None,
            "pii_nonce": pii.nonce if pii else None,
            "pii_key_version": pii.key_version if pii else None,
            "degree_level": profile.degree_level,
            "major": profile.major,
            "institution": profile.institution,
            "gpa": profile.gpa.value if profile.gpa else None,
            "gpa_scale": profile.gpa.scale if profile.gpa else None,
            "nationality": profile.nationality,
            "country_of_residence": profile.country_of_residence,
            "graduation_year": profile.graduation_year,
            "skills": profile.skills,
            "languages": [{"name": lang.name, "level": lang.level} for lang in profile.languages],
            "experiences": [
                {
                    "title": e.title,
                    "organization": e.organization,
                    "start": e.start,
                    "end": e.end,
                    "description": e.description,
                }
                for e in profile.experiences
            ],
            "details": profile.details.to_dict(),
            "updated_at": profile.updated_at,
        }
        stmt = pg_insert(StudentProfileRow).values(
            id=profile.id, user_id=profile.user_id, created_at=profile.created_at, **values
        )
        await self._s.execute(
            stmt.on_conflict_do_update(constraint="uq_student_profiles_user", set_=values)
        )


# ---- documents ---------------------------------------------------------------------------------


def _document(row: DocumentRow) -> Document:
    return Document(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_key=row.storage_key,
        page_count=row.page_count,
        text_chars=row.text_chars,
        created_at=row.created_at,
    )


class SqlDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, stored: StoredDocument) -> None:
        d = stored.document
        self._s.add(
            DocumentRow(
                id=d.id,
                user_id=d.user_id,
                kind=d.kind,
                filename=d.filename,
                mime=d.mime,
                size_bytes=d.size_bytes,
                sha256=d.sha256,
                storage_key=d.storage_key,
                blob_nonce=stored.blob_nonce,
                blob_key_version=stored.blob_key_version,
                page_count=d.page_count,
                text_ciphertext=stored.text.ciphertext if stored.text else None,
                text_nonce=stored.text.nonce if stored.text else None,
                text_key_version=stored.text.key_version if stored.text else None,
                text_chars=d.text_chars,
                created_at=d.created_at,
            )
        )
        await self._s.flush()

    async def list(self, user_id: uuid.UUID) -> list[Document]:
        rows = await self._s.scalars(
            select(DocumentRow)
            .where(DocumentRow.user_id == user_id)
            .order_by(DocumentRow.created_at.desc())
        )
        return [_document(r) for r in rows]

    async def get(self, user_id: uuid.UUID, document_id: uuid.UUID) -> StoredDocument | None:
        row = await self._s.scalar(
            select(DocumentRow).where(DocumentRow.user_id == user_id, DocumentRow.id == document_id)
        )
        if row is None:
            return None
        secret = None
        if row.text_ciphertext and row.text_nonce and row.text_key_version is not None:
            secret = EncryptedSecret(
                ciphertext=row.text_ciphertext,
                nonce=row.text_nonce,
                key_version=row.text_key_version,
            )
        return StoredDocument(
            document=_document(row),
            blob_nonce=row.blob_nonce,
            blob_key_version=row.blob_key_version,
            text=secret,
        )

    async def delete(self, user_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(DocumentRow).where(DocumentRow.user_id == user_id, DocumentRow.id == document_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def find_by_sha(self, user_id: uuid.UUID, sha256: str) -> Document | None:
        row = await self._s.scalar(
            select(DocumentRow).where(DocumentRow.user_id == user_id, DocumentRow.sha256 == sha256)
        )
        return _document(row) if row else None


# ---- essays + fingerprints ---------------------------------------------------------------------


def _essay(row: ReferenceEssayRow) -> ReferenceEssay:
    return ReferenceEssay(
        id=row.id,
        user_id=row.user_id,
        title=row.title,
        body=row.body,
        prompt_context=row.prompt_context,
        word_count=row.word_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlEssayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, user_id: uuid.UUID) -> list[ReferenceEssay]:
        rows = await self._s.scalars(
            select(ReferenceEssayRow)
            .where(ReferenceEssayRow.user_id == user_id)
            .order_by(ReferenceEssayRow.created_at)
        )
        return [_essay(r) for r in rows]

    async def get(self, user_id: uuid.UUID, essay_id: uuid.UUID) -> ReferenceEssay | None:
        row = await self._s.scalar(
            select(ReferenceEssayRow).where(
                ReferenceEssayRow.user_id == user_id, ReferenceEssayRow.id == essay_id
            )
        )
        return _essay(row) if row else None

    async def add(self, essay: ReferenceEssay) -> None:
        self._s.add(
            ReferenceEssayRow(
                id=essay.id,
                user_id=essay.user_id,
                title=essay.title,
                body=essay.body,
                prompt_context=essay.prompt_context,
                word_count=essay.word_count,
                created_at=essay.created_at,
                updated_at=essay.updated_at,
            )
        )
        await self._s.flush()

    async def update(self, essay: ReferenceEssay) -> None:
        await self._s.execute(
            update(ReferenceEssayRow)
            .where(ReferenceEssayRow.user_id == essay.user_id, ReferenceEssayRow.id == essay.id)
            .values(
                title=essay.title,
                body=essay.body,
                prompt_context=essay.prompt_context,
                word_count=essay.word_count,
                updated_at=essay.updated_at,
            )
        )

    async def delete(self, user_id: uuid.UUID, essay_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(ReferenceEssayRow).where(
                ReferenceEssayRow.user_id == user_id, ReferenceEssayRow.id == essay_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def replace_chunks(
        self,
        user_id: uuid.UUID,
        essay_id: uuid.UUID,
        chunks: Sequence[EmbeddedChunk],
        embedding_model: str,
    ) -> None:
        await self._s.execute(
            text("DELETE FROM essay_chunks WHERE user_id = :u AND essay_id = :e"),
            {"u": user_id, "e": essay_id},
        )
        for chunk in chunks:
            await self._s.execute(
                text(
                    "INSERT INTO essay_chunks "
                    "(id, user_id, essay_id, ordinal, text, embedding, embedding_model) "
                    "VALUES (:id, :u, :e, :o, :t, CAST(CAST(:v AS text) AS vector), :m)"
                ),
                {
                    "id": new_uuid7(),
                    "u": user_id,
                    "e": essay_id,
                    "o": chunk.ordinal,
                    "t": chunk.text,
                    "v": vector_literal(chunk.vector),
                    "m": embedding_model,
                },
            )

    async def nearest_chunks(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> builtins.list[tuple[str, float]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH mine AS MATERIALIZED (
                        SELECT text, embedding FROM essay_chunks
                        WHERE user_id = :u AND embedding_model = :m
                    )
                    SELECT text, 1 - (embedding <=> CAST(CAST(:v AS text) AS vector)) AS sim
                    FROM mine ORDER BY embedding <=> CAST(CAST(:v AS text) AS vector) LIMIT :k
                    """
                ),
                {"u": user_id, "m": embedding_model, "v": vector_literal(vector), "k": k},
            )
        ).all()
        return [(r.text, float(r.sim)) for r in rows]

    async def chunk_count(self, user_id: uuid.UUID) -> int:
        count = await self._s.scalar(
            text("SELECT count(*) FROM essay_chunks WHERE user_id = :u"), {"u": user_id}
        )
        return int(count or 0)


class SqlDocumentChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def replace_chunks(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[EmbeddedChunk],
        embedding_model: str,
    ) -> None:
        await self._s.execute(
            text("DELETE FROM document_chunks WHERE user_id = :u AND document_id = :d"),
            {"u": user_id, "d": document_id},
        )
        for chunk in chunks:
            await self._s.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(id, user_id, document_id, ordinal, text, embedding, embedding_model) "
                    "VALUES (:id, :u, :d, :o, :t, CAST(CAST(:v AS text) AS vector), :m)"
                ),
                {
                    "id": new_uuid7(),
                    "u": user_id,
                    "d": document_id,
                    "o": chunk.ordinal,
                    "t": chunk.text,
                    "v": vector_literal(chunk.vector),
                    "m": embedding_model,
                },
            )

    async def nearest(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> builtins.list[DocumentHit]:
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH mine AS MATERIALIZED (
                        SELECT document_id, text, embedding FROM document_chunks
                        WHERE user_id = :u AND embedding_model = :m
                    )
                    SELECT document_id, text,
                           1 - (embedding <=> CAST(CAST(:v AS text) AS vector)) AS sim
                    FROM mine ORDER BY embedding <=> CAST(CAST(:v AS text) AS vector) LIMIT :k
                    """
                ),
                {"u": user_id, "m": embedding_model, "v": vector_literal(vector), "k": k},
            )
        ).all()
        return [DocumentHit(r.document_id, r.text, float(r.sim)) for r in rows]

    async def indexed_documents(self, user_id: uuid.UUID, embedding_model: str) -> set[uuid.UUID]:
        rows = await self._s.execute(
            text(
                "SELECT DISTINCT document_id FROM document_chunks "
                "WHERE user_id = :u AND embedding_model = :m"
            ),
            {"u": user_id, "m": embedding_model},
        )
        return {r.document_id for r in rows}

    async def count(self, user_id: uuid.UUID, document_id: uuid.UUID) -> int:
        n = await self._s.scalar(
            text("SELECT count(*) FROM document_chunks WHERE user_id = :u AND document_id = :d"),
            {"u": user_id, "d": document_id},
        )
        return int(n or 0)


class SqlFingerprintRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, fingerprint: StyleFingerprint) -> None:
        self._s.add(
            StyleFingerprintRow(
                id=fingerprint.id,
                user_id=fingerprint.user_id,
                version=fingerprint.version,
                essay_count=fingerprint.essay_count,
                total_words=fingerprint.total_words,
                metrics=fingerprint.metrics,
                feature_names=fingerprint.feature_names,
                feature_vector=fingerprint.feature_vector,
                created_at=fingerprint.created_at,
            )
        )
        await self._s.flush()

    async def latest(self, user_id: uuid.UUID) -> StyleFingerprint | None:
        row = await self._s.scalar(
            select(StyleFingerprintRow)
            .where(StyleFingerprintRow.user_id == user_id)
            .order_by(StyleFingerprintRow.version.desc())
            .limit(1)
        )
        if row is None:
            return None
        return StyleFingerprint(
            id=row.id,
            user_id=row.user_id,
            version=row.version,
            essay_count=row.essay_count,
            total_words=row.total_words,
            metrics=dict(row.metrics),
            feature_names=list(row.feature_names),
            feature_vector=[float(x) for x in row.feature_vector],
            created_at=row.created_at,
        )

    async def next_version(self, user_id: uuid.UUID) -> int:
        current = await self._s.scalar(
            select(func.max(StyleFingerprintRow.version)).where(
                StyleFingerprintRow.user_id == user_id
            )
        )
        return int(current or 0) + 1


# ---- semantic memory ---------------------------------------------------------------------------


def _qa(row: KnowledgeMemoryRow) -> KnowledgeQA:
    return KnowledgeQA(
        id=row.id,
        user_id=row.user_id,
        question_text=row.question_text,
        question_normalized=row.question_normalized,
        answer_text=row.answer_text,
        embedding_model=row.embedding_model,
        source=row.source,
        times_reused=row.times_reused,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# Exact cosine ranking inside the tenant's rows. The MATERIALIZED CTE filters by user_id (btree)
# first, so results are exact and never starved by other tenants' rows; this is the correct plan
# on pgvector 0.6.x, which lacks 0.8's iterative HNSW scans. The HNSW index serves large
# cross-row workloads and future versions.
_NEAREST_SQL = text(
    """
    WITH mine AS MATERIALIZED (
        SELECT id, embedding FROM knowledge_memory_qa
        WHERE user_id = :u AND embedding_model = :m
    )
    SELECT id, 1 - (embedding <=> CAST(CAST(:v AS text) AS vector)) AS similarity
    FROM mine
    ORDER BY embedding <=> CAST(CAST(:v AS text) AS vector)
    LIMIT :k
    """
)


class SqlMemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, qa: KnowledgeQA, vector: Sequence[float]) -> None:
        await self._s.execute(
            text(
                "INSERT INTO knowledge_memory_qa (id, user_id, question_text, "
                "question_normalized, answer_text, embedding, embedding_model, source, "
                "times_reused, last_used_at, created_at, updated_at) VALUES (:id, :u, :q, :qn, "
                ":a, CAST(CAST(:v AS text) AS vector), :m, CAST(:s AS memory_source), :tr, :lu, "
                ":ca, :ua)"
            ),
            {
                "id": qa.id,
                "u": qa.user_id,
                "q": qa.question_text,
                "qn": qa.question_normalized,
                "a": qa.answer_text,
                "v": vector_literal(vector),
                "m": qa.embedding_model,
                "s": qa.source.value,
                "tr": qa.times_reused,
                "lu": qa.last_used_at,
                "ca": qa.created_at,
                "ua": qa.updated_at,
            },
        )

    async def get(self, user_id: uuid.UUID, qa_id: uuid.UUID) -> KnowledgeQA | None:
        row = await self._s.scalar(
            select(KnowledgeMemoryRow).where(
                KnowledgeMemoryRow.user_id == user_id, KnowledgeMemoryRow.id == qa_id
            )
        )
        return _qa(row) if row else None

    async def page(
        self, user_id: uuid.UUID, search: str | None, limit: int, offset: int
    ) -> tuple[list[KnowledgeQA], int]:
        where = [KnowledgeMemoryRow.user_id == user_id]
        if search:
            pattern = f"%{search.replace('%', r'\%').replace('_', r'\_')}%"
            where.append(
                or_(
                    KnowledgeMemoryRow.question_text.ilike(pattern),
                    KnowledgeMemoryRow.answer_text.ilike(pattern),
                )
            )
        total = await self._s.scalar(
            select(func.count()).select_from(KnowledgeMemoryRow).where(*where)
        )
        rows = await self._s.scalars(
            select(KnowledgeMemoryRow)
            .where(*where)
            .order_by(KnowledgeMemoryRow.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_qa(r) for r in rows], int(total or 0)

    async def update(self, qa: KnowledgeQA, vector: Sequence[float] | None) -> None:
        await self._s.execute(
            update(KnowledgeMemoryRow)
            .where(KnowledgeMemoryRow.user_id == qa.user_id, KnowledgeMemoryRow.id == qa.id)
            .values(
                question_text=qa.question_text,
                question_normalized=qa.question_normalized,
                answer_text=qa.answer_text,
                embedding_model=qa.embedding_model,
                updated_at=qa.updated_at,
            )
        )
        if vector is not None:
            await self._s.execute(
                text(
                    "UPDATE knowledge_memory_qa SET embedding = CAST(CAST(:v AS text) AS vector) "
                    "WHERE user_id = :u AND id = :id"
                ),
                {"v": vector_literal(vector), "u": qa.user_id, "id": qa.id},
            )

    async def delete(self, user_id: uuid.UUID, qa_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(KnowledgeMemoryRow).where(
                KnowledgeMemoryRow.user_id == user_id, KnowledgeMemoryRow.id == qa_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def find_question(
        self, user_id: uuid.UUID, question_normalized: str
    ) -> KnowledgeQA | None:
        row = await self._s.scalar(
            select(KnowledgeMemoryRow).where(
                KnowledgeMemoryRow.user_id == user_id,
                KnowledgeMemoryRow.question_normalized == question_normalized,
            )
        )
        return _qa(row) if row else None

    async def nearest(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> list[ScoredQA]:
        scored = (
            await self._s.execute(
                _NEAREST_SQL,
                {"u": user_id, "m": embedding_model, "v": vector_literal(vector), "k": k},
            )
        ).all()
        if not scored:
            return []
        ids = [row.id for row in scored]
        rows = {
            r.id: r
            for r in await self._s.scalars(
                select(KnowledgeMemoryRow).where(
                    KnowledgeMemoryRow.user_id == user_id, KnowledgeMemoryRow.id.in_(ids)
                )
            )
        }
        return [
            ScoredQA(qa=_qa(rows[row.id]), similarity=float(row.similarity))
            for row in scored
            if row.id in rows
        ]

    async def count_by_model(self, user_id: uuid.UUID, embedding_model: str) -> tuple[int, int]:
        row = (
            await self._s.execute(
                text(
                    "SELECT count(*) FILTER (WHERE embedding_model = :m) AS same, "
                    "count(*) FILTER (WHERE embedding_model <> :m) AS other "
                    "FROM knowledge_memory_qa WHERE user_id = :u"
                ),
                {"u": user_id, "m": embedding_model},
            )
        ).one()
        return int(row.same), int(row.other)
