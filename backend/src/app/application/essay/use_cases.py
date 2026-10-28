from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.vault_deps import VaultDeps
from app.domain.essay.models import (
    MAX_REFERENCE_ESSAYS,
    ReferenceEssay,
    StyleFingerprint,
    chunk_essay,
    word_count,
)
from app.domain.events.models import NewEvent
from app.domain.ports.uow import IUnitOfWork
from app.domain.ports.vault import EmbeddedChunk
from app.domain.shared.enums import Actor
from app.domain.shared.errors import ConflictError, NotFoundError
from app.domain.shared.ids import new_uuid7

TABLE = "reference_essays"


@dataclass(frozen=True, slots=True)
class EssayInput:
    title: str
    body: str
    prompt_context: str | None = None


async def _embed_chunks(
    deps: VaultDeps, user_id: uuid.UUID, body: str
) -> tuple[list[EmbeddedChunk], str]:
    """Embed paragraph chunks as retrieval passages (outside any DB transaction)."""
    texts = chunk_essay(body)
    provider = await deps.embeddings.execute(user_id)
    vectors = await provider.embed_documents(texts)
    chunks = [
        EmbeddedChunk(ordinal=i, text=t, vector=v)
        for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
    ]
    return chunks, provider.model_name


async def _recompute_fingerprint(
    deps: VaultDeps, uow: IUnitOfWork, user_id: uuid.UUID
) -> StyleFingerprint | None:
    """New fingerprint version over ALL current reference essays (same transaction)."""
    essays = await uow.essays.list(user_id)
    if not essays:
        return None
    analysis = deps.stylometry.analyze([e.body for e in essays])
    fingerprint = StyleFingerprint(
        id=new_uuid7(),
        user_id=user_id,
        version=await uow.fingerprints.next_version(user_id),
        essay_count=len(essays),
        total_words=sum(e.word_count for e in essays),
        metrics=analysis.metrics,
        feature_names=analysis.feature_names,
        feature_vector=analysis.feature_vector,
        created_at=deps.clock.now(),
    )
    await uow.fingerprints.add(fingerprint)
    sentence = analysis.metrics.get("sentence_length", {})
    await uow.events.append(
        NewEvent(
            user_id=user_id,
            type="essay.fingerprint_updated",
            payload={
                "version": fingerprint.version,
                "essay_count": fingerprint.essay_count,
                "burstiness": sentence.get("burstiness"),
                "mtld": analysis.metrics.get("lexical", {}).get("mtld"),
            },
        )
    )
    return fingerprint


@dataclass(slots=True)
class ListEssays:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID) -> list[ReferenceEssay]:
        async with self.deps.uow_factory(user_id) as uow:
            return await uow.essays.list(user_id)


@dataclass(slots=True)
class CreateEssay:
    deps: VaultDeps

    async def execute(
        self, user_id: uuid.UUID, data: EssayInput
    ) -> tuple[ReferenceEssay, StyleFingerprint | None]:
        now = self.deps.clock.now()
        essay = ReferenceEssay(
            id=new_uuid7(),
            user_id=user_id,
            title=data.title.strip()[:200],
            body=data.body.strip(),
            prompt_context=(data.prompt_context or "").strip()[:1000] or None,
            word_count=word_count(data.body),
            created_at=now,
            updated_at=now,
        )
        essay.validate()
        async with self.deps.uow_factory(user_id) as uow:
            if len(await uow.essays.list(user_id)) >= MAX_REFERENCE_ESSAYS:
                raise ConflictError(
                    f"at most {MAX_REFERENCE_ESSAYS} reference essays; delete one first"
                )
        chunks, model = await _embed_chunks(self.deps, user_id, essay.body)
        async with self.deps.uow_factory(user_id) as uow:
            if len(await uow.essays.list(user_id)) >= MAX_REFERENCE_ESSAYS:
                raise ConflictError(f"at most {MAX_REFERENCE_ESSAYS} reference essays")
            await uow.essays.add(essay)
            await uow.essays.replace_chunks(user_id, essay.id, chunks, model)
            fingerprint = await _recompute_fingerprint(self.deps, uow, user_id)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="essay.created",
                entity=TABLE,
                entity_id=essay.id,
            )
            await uow.commit()
        return essay, fingerprint


@dataclass(slots=True)
class UpdateEssay:
    deps: VaultDeps

    async def execute(
        self, user_id: uuid.UUID, essay_id: uuid.UUID, data: EssayInput
    ) -> tuple[ReferenceEssay, StyleFingerprint | None]:
        async with self.deps.uow_factory(user_id) as uow:
            essay = await uow.essays.get(user_id, essay_id)
        if essay is None:
            raise NotFoundError("essay not found")
        body_changed = data.body.strip() != essay.body
        essay.title = data.title.strip()[:200]
        essay.body = data.body.strip()
        essay.prompt_context = (data.prompt_context or "").strip()[:1000] or None
        essay.word_count = word_count(essay.body)
        essay.updated_at = self.deps.clock.now()
        essay.validate()
        chunks, model = (
            await _embed_chunks(self.deps, user_id, essay.body) if body_changed else ([], "")
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.essays.update(essay)
            fingerprint = None
            if body_changed:
                await uow.essays.replace_chunks(user_id, essay.id, chunks, model)
                fingerprint = await _recompute_fingerprint(self.deps, uow, user_id)
            await uow.commit()
        return essay, fingerprint


@dataclass(slots=True)
class DeleteEssay:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, essay_id: uuid.UUID) -> StyleFingerprint | None:
        async with self.deps.uow_factory(user_id) as uow:
            if not await uow.essays.delete(user_id, essay_id):
                raise NotFoundError("essay not found")
            fingerprint = await _recompute_fingerprint(self.deps, uow, user_id)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="essay.deleted",
                entity=TABLE,
                entity_id=essay_id,
            )
            await uow.commit()
        return fingerprint


@dataclass(frozen=True, slots=True)
class FingerprintView:
    fingerprint: StyleFingerprint | None
    essay_count: int
    chunk_count: int


@dataclass(slots=True)
class GetFingerprint:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID) -> FingerprintView:
        async with self.deps.uow_factory(user_id) as uow:
            essays = await uow.essays.list(user_id)
            fingerprint = await uow.fingerprints.latest(user_id) if essays else None
            chunks = await uow.essays.chunk_count(user_id)
        return FingerprintView(fingerprint=fingerprint, essay_count=len(essays), chunk_count=chunks)
