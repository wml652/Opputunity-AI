"""Unified personal-evidence RAG for portal questions the profile cannot answer directly.

Before a custom question pauses an application (PAUSED_NEEDS_USER_INPUT), search the student's
own evidence - past Q&A answers, the uploaded CV / transcripts / certificates (document_chunks)
and the reference essays (essay_chunks) - and let the user's model synthesize a concise answer
grounded STRICTLY in that evidence. It is used only when the combined confidence (model
confidence x grounding check) reaches 0.85; otherwise the question still goes to the Inbox, now
with the best suggestion attached.

Every snippet is PII-redacted before the model sees it (constitution: the LLM never receives
PII), and the redacted snippet is stored as the field's evidence for review."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.application.llm.use_cases import GetEmbeddingProvider, ResolveChatProvider
from app.application.profile.indexing import IndexMissingDocuments
from app.domain.ports.llm import ILLMProvider
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.ports.vault import IEvidenceAnswerer
from app.domain.profile.evidence import (
    EvidenceAnswer,
    EvidenceSnippet,
    combine_confidence,
    grounding_overlap,
    redact_pii,
)
from app.domain.shared.errors import DomainError

TOP_DOCUMENT_CHUNKS = 5
TOP_ESSAY_CHUNKS = 3


@dataclass(frozen=True, slots=True)
class EvidenceDeps:
    uow_factory: IUnitOfWorkFactory
    embeddings: GetEmbeddingProvider
    chat: ResolveChatProvider
    answerer: IEvidenceAnswerer
    backfill: IndexMissingDocuments


@dataclass(slots=True)
class ProfileEvidenceRetriever:
    deps: EvidenceDeps

    async def retrieve(
        self,
        user_id: uuid.UUID,
        question: str,
        *,
        pii_values: Sequence[str] = (),
        memory: Sequence[tuple[str, str, float]] = (),
    ) -> list[EvidenceSnippet]:
        """Redacted evidence for `question`, best first: CV/transcript chunks, essay passages
        and close past answers (`memory`: (question, answer, similarity))."""
        await self.deps.backfill.execute(user_id)  # index any document not indexed yet
        embedder = await self.deps.embeddings.execute(user_id)
        vector = await embedder.embed_query(question)
        async with self.deps.uow_factory(user_id) as uow:
            docs = await uow.document_chunks.nearest(
                user_id, vector, embedder.model_name, TOP_DOCUMENT_CHUNKS
            )
            essays = await uow.essays.nearest_chunks(
                user_id, vector, embedder.model_name, TOP_ESSAY_CHUNKS
            )
        snippets = [
            EvidenceSnippet(
                "DOCUMENT", redact_pii(d.text, pii_values), d.similarity, str(d.document_id)
            )
            for d in docs
        ]
        snippets += [
            EvidenceSnippet("ESSAY", redact_pii(text, pii_values), sim) for text, sim in essays
        ]
        snippets += [
            EvidenceSnippet("MEMORY", redact_pii(f"Q: {q}\nA: {a}", pii_values), sim)
            for q, a, sim in memory
        ]
        return sorted(snippets, key=lambda s: -s.similarity)

    async def answer(
        self,
        user_id: uuid.UUID,
        question: str,
        *,
        options: Sequence[str] = (),
        pii_values: Sequence[str] = (),
        memory: Sequence[tuple[str, str, float]] = (),
        llm: ILLMProvider | None = None,
    ) -> EvidenceAnswer:
        snippets = await self.retrieve(user_id, question, pii_values=pii_values, memory=memory)
        if not snippets:
            return EvidenceAnswer(None, 0.0, None, None, False, "no evidence in the vault")
        model = llm or await self.deps.chat.execute(user_id)
        try:
            reply = await self.deps.answerer.answer(
                model, redact_pii(question, pii_values), list(options), snippets
            )
        except DomainError as exc:
            return EvidenceAnswer(None, 0.0, None, None, False, f"model error: {exc.message}")
        if not reply.answerable or not reply.answer:
            return EvidenceAnswer(None, 0.0, None, None, False, "the evidence does not answer it")
        cited = [snippets[i] for i in reply.cited if 0 <= i < len(snippets)] or snippets[:3]
        overlap = grounding_overlap(reply.answer, [s.text for s in cited])
        confidence = combine_confidence(reply.confidence, overlap)
        answer = redact_pii(reply.answer, pii_values)  # an answer never carries PII either
        return EvidenceAnswer(
            answer=answer,
            confidence=confidence,
            source=cited[0].source,
            snippet=cited[0].text[:600],
            grounded=overlap >= 0.6,
            reason=f"grounding {overlap:.2f}, model {reply.confidence:.2f}",
        )
