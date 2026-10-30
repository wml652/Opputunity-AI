"""Ports for the student vault (profile, documents, essays, memory)."""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.essay.models import ReferenceEssay, StyleAnalysis, StyleFingerprint
from app.domain.llm.provider import EncryptedSecret
from app.domain.memory.models import KnowledgeQA
from app.domain.ports.llm import ILLMProvider
from app.domain.profile.documents import Document, ExtractedText
from app.domain.profile.evidence import EvidenceSnippet
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

    async def nearest_chunks(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> builtins.list[tuple[str, float]]:
        """(chunk text, cosine similarity) of the user's reference essays."""
        ...


@dataclass(frozen=True, slots=True)
class DocumentHit:
    document_id: uuid.UUID
    text: str
    similarity: float


class IDocumentChunkRepository(Protocol):
    async def replace_chunks(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[EmbeddedChunk],
        embedding_model: str,
    ) -> None: ...

    async def nearest(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str, k: int
    ) -> builtins.list[DocumentHit]:
        """The user's document chunks closest to `vector` (exact per-tenant ranking)."""
        ...

    async def indexed_documents(
        self, user_id: uuid.UUID, embedding_model: str
    ) -> set[uuid.UUID]: ...

    async def count(self, user_id: uuid.UUID, document_id: uuid.UUID) -> int: ...


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


@dataclass(frozen=True, slots=True)
class EvidenceReply:
    answerable: bool
    answer: str | None
    confidence: float  # the model's own confidence in [0, 1]
    cited: list[int]  # indices into the snippets it was given


class IEvidenceAnswerer(Protocol):
    async def answer(
        self,
        llm: ILLMProvider,
        question: str,
        options: list[str],
        snippets: Sequence[EvidenceSnippet],
    ) -> EvidenceReply:
        """Answer ONE form question strictly from (already PII-redacted) evidence snippets."""
        ...
