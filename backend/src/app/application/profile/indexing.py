"""Index the student's uploaded documents (CV, transcript, certificates) for evidence retrieval.

Chunks are stored as written (they are the user's own data, protected by RLS); PII is redacted
later, at the moment a snippet is put in front of a model or stored as answer evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.vault_deps import VaultDeps
from app.domain.ports.crypto import aad
from app.domain.ports.vault import EmbeddedChunk
from app.domain.profile.evidence import chunk_document

DOCUMENT_TABLE = "documents"
MAX_CHUNKS_PER_DOCUMENT = 120


@dataclass(slots=True)
class IndexDocument:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, document_id: uuid.UUID) -> int:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.documents.get(user_id, document_id)
        if stored is None or stored.text is None:
            return 0
        text = self.deps.cipher.decrypt(
            stored.text, aad(DOCUMENT_TABLE, "extracted_text", user_id, document_id)
        ).decode()
        chunks = chunk_document(text)[:MAX_CHUNKS_PER_DOCUMENT]
        embedder = await self.deps.embeddings.execute(user_id)
        vectors = await embedder.embed_documents(chunks) if chunks else []
        async with self.deps.uow_factory(user_id) as uow:
            await uow.document_chunks.replace_chunks(
                user_id,
                document_id,
                [
                    EmbeddedChunk(i, c, v)
                    for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
                ],
                embedder.model_name,
            )
            await uow.commit()
        return len(chunks)


@dataclass(slots=True)
class IndexMissingDocuments:
    """Backfill: index every document not yet indexed with the CURRENT embedding model (covers
    documents uploaded before this feature and a changed embedding model)."""

    index: IndexDocument

    async def execute(self, user_id: uuid.UUID) -> int:
        deps = self.index.deps
        embedder = await deps.embeddings.execute(user_id)
        async with deps.uow_factory(user_id) as uow:
            documents = await uow.documents.list(user_id)
            indexed = await uow.document_chunks.indexed_documents(user_id, embedder.model_name)
        done = 0
        for document in documents:
            if document.id not in indexed and document.text_chars > 0:
                done += 1 if await self.index.execute(user_id, document.id) else 0
        return done
