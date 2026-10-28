"""Ports for the student vault (profile, documents, essays, memory)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.essay.models import ReferenceEssay, StyleAnalysis, StyleFingerprint
from app.domain.llm.provider import EncryptedSecret
from app.domain.memory.models import KnowledgeQA
from app.domain.profile.documents import Document, ExtractedText
from app.domain.profile.models import StudentProfile


class IObjectStorage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class IPdfTextExtractor(Protocol):
    async def extract(self, data: bytes) -> ExtractedText:
        """Raise `UnsupportedFileError` if the PDF cannot be parsed."""
        ...


class IStylometryAnalyzer(Protocol):
    def analyze(self, texts: Sequence[str]) -> StyleAnalysis: ...


# ---- repositories ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredProfile:
    profile: StudentProfile  # `pii` left empty; decrypted by the use case
    pii: EncryptedSecret | None


class IProfileRepository(Protocol):
    async def get(self, user_id: uuid.UUID) -> StoredProfile | None: ...

    async def save(self, profile: StudentProfile, pii: EncryptedSecret | None) -> None:
        """Insert or update (one profile per user)."""
        ...


@dataclass(frozen=True, slots=True)
class StoredDocument:
    document: Document
    blob_nonce: bytes
    blob_key_version: int
    text: EncryptedSecret | None


class IDocumentRepository(Protocol):
    async def add(self, stored: StoredDocument) -> None: ...

    async def list(self, user_id: uuid.UUID) -> list[Document]: ...

    async def get(self, user_id: uuid.UUID, document_id: uuid.UUID) -> StoredDocument | None: ...

    async def delete(self, user_id: uuid.UUID, document_id: uuid.UUID) -> bool: ...

    async def find_by_sha(self, user_id: uuid.UUID, sha256: str) -> Document | None: ...


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    ordinal: int
    text: str
    vector: list[float]


class IEssayRepository(Protocol):
    async def list(self, user_id: uuid.UUID) -> list[ReferenceEssay]: ...

    async def get(self, user_id: uuid.UUID, essay_id: uuid.UUID) -> ReferenceEssay | None: ...

    async def add(self, essay: ReferenceEssay) -> None: ...

    async def update(self, essay: ReferenceEssay) -> None: ...

    async def delete(self, user_id: uuid.UUID, essay_id: uuid.UUID) -> bool: ...

    async def replace_chunks(
        self,
        user_id: uuid.UUID,
        essay_id: uuid.UUID,
        chunks: Sequence[EmbeddedChunk],
        embedding_model: str,
    ) -> None: ...

    async def chunk_count(self, user_id: uuid.UUID) -> int: ...


class IFingerprintRepository(Protocol):
    async def add(self, fingerprint: StyleFingerprint) -> None: ...

    async def latest(self, user_id: uuid.UUID) -> StyleFingerprint | None: ...

    async def next_version(self, user_id: uuid.UUID) -> int: ...


@dataclass(frozen=True, slots=True)
class ScoredQA:
    qa: KnowledgeQA
    similarity: float  # 1 - cosine distance


class IMemoryRepository(Protocol):
    async def add(self, qa: KnowledgeQA, vector: Sequence[float]) -> None: ...

    async def get(self, user_id: uuid.UUID, qa_id: uuid.UUID) -> KnowledgeQA | None: ...

    async def page(
        self, user_id: uuid.UUID, search: str | None, limit: int, offset: int
    ) -> tuple[list[KnowledgeQA], int]: ...

    async def update(self, qa: KnowledgeQA, vector: Sequence[float] | None) -> None: ...

    async def delete(self, user_id: uuid.UUID, qa_id: uuid.UUID) -> bool: ...

    async def find_question(
        self, user_id: uuid.UUID, question_normalized: str
    ) -> KnowledgeQA | None: ...

    async def nearest(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> list[ScoredQA]: ...

    async def count_by_model(self, user_id: uuid.UUID, embedding_model: str) -> tuple[int, int]:
        """(rows embedded with `embedding_model`, rows embedded with any other model)."""
        ...
