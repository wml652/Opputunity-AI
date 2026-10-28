from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.vault_deps import VaultDeps
from app.domain.events.models import NewEvent
from app.domain.memory.models import (
    AUTO_FILL_THRESHOLD,
    ConfidenceScore,
    KnowledgeQA,
    MemorySource,
    RecallMatch,
    RecallResult,
    normalize_question,
)
from app.domain.shared.enums import Actor
from app.domain.shared.errors import ConflictError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

TABLE = "knowledge_memory_qa"
MAX_RECALL_K = 20


@dataclass(slots=True)
class ListMemory:
    deps: VaultDeps

    async def execute(
        self, user_id: uuid.UUID, *, search: str | None, limit: int = 50, offset: int = 0
    ) -> tuple[list[KnowledgeQA], int]:
        async with self.deps.uow_factory(user_id) as uow:
            return await uow.memory.page(user_id, (search or "").strip() or None, limit, offset)


@dataclass(slots=True)
class LearnAnswer:
    """Store a Q&A pair with its question embedding.

    Memory matches a question against stored questions, so both sides are embedded the same way
    (`embed_query`). Measured on nemotron-3.5-embed-1b: identical/reworded questions score
    0.94-1.00, loose paraphrases 0.45-0.60, unrelated 0.13-0.17, a clean split at 0.85. The
    asymmetric passage/query mode scored an identical question at only 0.68 (never auto-fills)."""

    deps: VaultDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        *,
        question: str,
        answer: str,
        source: MemorySource = MemorySource.USER,
    ) -> KnowledgeQA:
        now = self.deps.clock.now()
        provider = await self.deps.embeddings.execute(user_id)
        qa = KnowledgeQA(
            id=new_uuid7(),
            user_id=user_id,
            question_text=question.strip(),
            question_normalized=normalize_question(question),
            answer_text=answer.strip(),
            embedding_model=provider.model_name,
            source=source,
            times_reused=0,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        qa.validate()
        async with self.deps.uow_factory(user_id) as uow:
            if await uow.memory.find_question(user_id, qa.question_normalized):
                raise ConflictError("this question is already in memory; edit the existing answer")
        vector = await provider.embed_query(qa.question_text)
        async with self.deps.uow_factory(user_id) as uow:
            await uow.memory.add(qa, vector)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="memory.learned",
                entity=TABLE,
                entity_id=qa.id,
            )
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="memory.learned",
                    payload={"qa_id": str(qa.id), "source": source.value},
                )
            )
            await uow.commit()
        return qa


@dataclass(slots=True)
class UpdateMemory:
    deps: VaultDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        qa_id: uuid.UUID,
        *,
        question: str | None = None,
        answer: str | None = None,
    ) -> KnowledgeQA:
        async with self.deps.uow_factory(user_id) as uow:
            qa = await uow.memory.get(user_id, qa_id)
        if qa is None:
            raise NotFoundError("memory entry not found")
        vector: list[float] | None = None
        if question is not None and normalize_question(question) != qa.question_normalized:
            normalized = normalize_question(question)
            async with self.deps.uow_factory(user_id) as uow:
                clash = await uow.memory.find_question(user_id, normalized)
            if clash is not None and clash.id != qa.id:
                raise ConflictError("another memory entry already has this question")
            provider = await self.deps.embeddings.execute(user_id)
            vector = await provider.embed_query(question.strip())
            qa.question_text = question.strip()
            qa.question_normalized = normalized
            qa.embedding_model = provider.model_name
        if answer is not None:
            qa.answer_text = answer.strip()
        qa.updated_at = self.deps.clock.now()
        qa.validate()
        async with self.deps.uow_factory(user_id) as uow:
            await uow.memory.update(qa, vector)
            await uow.commit()
        return qa


@dataclass(slots=True)
class DeleteMemory:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, qa_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            if not await uow.memory.delete(user_id, qa_id):
                raise NotFoundError("memory entry not found")
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="memory.deleted",
                entity=TABLE,
                entity_id=qa_id,
            )
            await uow.commit()


@dataclass(slots=True)
class RecallMemory:
    """Embed the question (symmetric with storage) and rank this tenant's memory by cosine
    similarity; the best match auto-fills only at or above the 0.85 threshold."""

    deps: VaultDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        question: str,
        *,
        k: int = 5,
        threshold: float = AUTO_FILL_THRESHOLD,
    ) -> RecallResult:
        if not question.strip():
            raise ValidationError("question is empty")
        k = max(1, min(k, MAX_RECALL_K))
        provider = await self.deps.embeddings.execute(user_id)
        vector = await provider.embed_query(question.strip())
        async with self.deps.uow_factory(user_id) as uow:
            scored = await uow.memory.nearest(user_id, vector, provider.model_name, k)
            searched, stale = await uow.memory.count_by_model(user_id, provider.model_name)
        return RecallResult(
            query=question.strip(),
            embedding_model=provider.model_name,
            threshold=threshold,
            matches=[
                RecallMatch(
                    qa=s.qa, confidence=ConfidenceScore.from_cosine(s.similarity, threshold)
                )
                for s in scored
            ],
            searched=searched,
            stale=stale,
        )
