"""Stage 2: voice-matched essay synthesis with a critic pass and deterministic scores.

1. Retrieve the top-k reference-essay chunks most similar to the prompt, the latest style
   fingerprint, and non-PII profile facts.
2. Generate with the USER'S configured model under an explicit style specification (target
   sentence-length mean/sigma and burstiness, the student's own transitions, word limit, banned
   cliches).
3. Critic: deterministic cliche scan + LLM grounding review (claims must trace to the profile or
   essays); revise once if anything is flagged.
4. Score deterministically: Voice Match = cosine of normalized stylometric feature vectors;
   Burstiness = 1 - |CV_draft - CV_student| / CV_student.

If the opportunity prohibits AI-written essays the draft is kept as a starting point but flagged
`requires_manual`: it cannot be auto-filled until the student authors/edits it.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.application.llm.use_cases import GetEmbeddingProvider, ResolveChatProvider
from app.domain.discovery.models import AiPolicy
from app.domain.eligibility.rules import profile_snapshot
from app.domain.essay.synthesis import (
    CLICHES,
    EssayDraft,
    burstiness_score,
    find_cliches,
    voice_match_score,
    word_limit_ok,
)
from app.domain.events.models import NewEvent
from app.domain.ports.llm import ChatMessage
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.ports.vault import IStylometryAnalyzer
from app.domain.shared.clock import Clock
from app.domain.shared.enums import EssayStatus
from app.domain.shared.errors import NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

TOP_K_CHUNKS = 4


@dataclass(frozen=True, slots=True)
class EssayDeps:
    uow_factory: IUnitOfWorkFactory
    clock: Clock
    chat: ResolveChatProvider
    embeddings: GetEmbeddingProvider
    stylometry: IStylometryAnalyzer


@dataclass(frozen=True, slots=True)
class EssayRequest:
    prompt: str
    word_limit: int | None = None
    application_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    field_key: str | None = None
    ai_policy: AiPolicy = AiPolicy.UNKNOWN
    organization: str | None = None


SYSTEM = """You write application essays IN THE VOICE OF ONE SPECIFIC STUDENT, as a draft they \
will review and edit. Match their style specification exactly. Use ONLY facts present in \
<student_facts> or <voice_samples>; never invent achievements, numbers, names, or experiences. \
If a fact the prompt asks for is missing, write around it honestly. Never use these words or \
phrases: {banned}. Output ONLY the essay text, no title, no preamble."""


def _style_spec(metrics: dict[str, Any], word_limit: int | None) -> str:
    s = metrics.get("sentence_length", {})
    transitions = [t["phrase"] for t in metrics.get("transitions", {}).get("top", [])][:8]
    para = metrics.get("paragraphs", {})
    grade = float(metrics.get("readability", {}).get("flesch_kincaid_grade", 10))
    return (
        f"- Sentence length: mean about {s.get('mean', 16):.0f} words, standard deviation about "
        f"{s.get('std', 7):.0f} (burstiness sigma/mu about {s.get('burstiness', 0.45):.2f}): "
        "mix short punchy sentences with longer ones the way the samples do.\n"
        f"- Paragraphs: about {para.get('words_mean', 90):.0f} words each.\n"
        f"- Transitions the student uses: {', '.join(transitions) or 'plain connectives'}.\n"
        f"- Reading level: Flesch-Kincaid grade about {grade:.0f}.\n"
        f"- Length: {'at most ' + str(word_limit) + ' words' if word_limit else 'about 350 words'}."
    )


@dataclass(slots=True)
class SynthesizeEssay:
    deps: EssayDeps

    async def execute(self, user_id: uuid.UUID, req: EssayRequest) -> EssayDraft:
        prompt = req.prompt.strip()
        if len(prompt) < 5:
            raise ValidationError("essay prompt is too short")
        llm = await self.deps.chat.execute(user_id)
        embedder = await self.deps.embeddings.execute(user_id)
        query = await embedder.embed_query(prompt)
        async with self.deps.uow_factory(user_id) as uow:
            fingerprint = await uow.fingerprints.latest(user_id)
            chunks = await uow.essays.nearest_chunks(
                user_id, query, embedder.model_name, TOP_K_CHUNKS
            )
            stored = await uow.profiles.get(user_id)
        if fingerprint is None or not chunks:
            raise ValidationError(
                "add reference essays first: the essay engine writes in YOUR voice"
            )
        facts = profile_snapshot(stored.profile) if stored else {}
        samples = "\n\n---\n\n".join(text for text, _ in chunks)
        banned = ", ".join(f'"{c}"' for c in CLICHES)
        user_msg = (
            f"<essay_prompt>{prompt}</essay_prompt>\n"
            + (f"<organization>{req.organization}</organization>\n" if req.organization else "")
            + "<style_specification>\n"
            + _style_spec(fingerprint.metrics, req.word_limit)
            + "\n</style_specification>\n"
            + f"<voice_samples>\n{samples}\n</voice_samples>\n"
            + f"<student_facts>{json.dumps(facts, ensure_ascii=False)}</student_facts>"
        )
        messages = [
            ChatMessage("system", SYSTEM.replace("{banned}", banned)),
            ChatMessage("user", user_msg),
        ]
        first = await llm.complete(messages, max_tokens=4096, temperature=0.7)
        body = _clean(first.text)
        critique = await self._critique(llm, body, facts, samples, req.word_limit)
        if critique["cliches"] or critique["unsupported_claims"] or not critique["within_limit"]:
            revision = (
                "Revise the essay. Remove these phrases entirely: "
                + (", ".join(critique["cliches"]) or "none")
                + ". Remove or soften these unsupported claims: "
                + ("; ".join(critique["unsupported_claims"]) or "none")
                + (f". Stay under {req.word_limit} words." if req.word_limit else ".")
                + " Keep the style specification. Output only the revised essay."
            )
            second = await llm.complete(
                [*messages, ChatMessage("assistant", body), ChatMessage("user", revision)],
                max_tokens=4096,
                temperature=0.5,
            )
            revised = _clean(second.text)
            if revised:
                body = revised
                critique = {
                    **await self._critique(llm, body, facts, samples, req.word_limit),
                    "revised": True,
                }
        scores = self.score(body, fingerprint.feature_vector, fingerprint.metrics)
        now = self.deps.clock.now()
        requires_manual = req.ai_policy is AiPolicy.PROHIBITED
        draft = EssayDraft(
            id=new_uuid7(),
            user_id=user_id,
            application_id=req.application_id,
            opportunity_id=req.opportunity_id,
            field_key=req.field_key,
            prompt=prompt,
            word_limit=req.word_limit,
            body=body,
            critique={
                **critique,
                **(
                    {
                        "policy": "This opportunity prohibits AI-written essays: "
                        "rewrite in your own words before it can be used."
                    }
                    if requires_manual
                    else {}
                ),
            },
            voice_match_score=scores["voice_match"],
            burstiness_score=scores["burstiness"],
            status=EssayStatus.CRITIQUED,
            requires_manual=requires_manual,
            model=llm.model,
            fingerprint_version=fingerprint.version,
            created_at=now,
            updated_at=now,
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.essay_drafts.add(draft)
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="essay.draft_created",
                    application_id=req.application_id,
                    payload={
                        "draft_id": str(draft.id),
                        "voice_match": draft.voice_match_score,
                        "burstiness": draft.burstiness_score,
                        "requires_manual": requires_manual,
                    },
                )
            )
            await uow.commit()
        return draft

    def score(
        self, body: str, student_vector: list[float], student_metrics: dict[str, Any]
    ) -> dict[str, float]:
        analysis = self.deps.stylometry.analyze([body])
        cv_student = float(student_metrics.get("sentence_length", {}).get("burstiness", 0.0))
        cv_draft = float(analysis.metrics["sentence_length"]["burstiness"])
        return {
            "voice_match": voice_match_score(student_vector, analysis.feature_vector),
            "burstiness": burstiness_score(cv_student, cv_draft),
            "draft_burstiness_cv": round(cv_draft, 4),
        }

    async def _critique(
        self, llm: Any, body: str, facts: dict[str, Any], samples: str, word_limit: int | None
    ) -> dict[str, Any]:
        unsupported: list[str] = []
        reply = await llm.complete(
            [
                ChatMessage(
                    "system",
                    "List claims in <essay> that are NOT supported by <student_facts> or "
                    '<voice_samples>. Reply ONLY with JSON: {"unsupported_claims": [str]} (max 5, '
                    "empty list if everything is grounded).",
                ),
                ChatMessage(
                    "user",
                    f"<essay>{body}</essay>\n<student_facts>{json.dumps(facts)}</student_facts>\n"
                    f"<voice_samples>{samples[:6000]}</voice_samples>",
                ),
            ],
            max_tokens=1024,
            temperature=0.0,
        )
        try:
            match = re.search(r"\{.*\}", reply.text, re.S)
            data = json.loads(match.group(0)) if match else {}
            unsupported = [str(c)[:200] for c in data.get("unsupported_claims", [])][:5]
        except (ValueError, AttributeError):
            unsupported = []
        return {
            "cliches": find_cliches(body),
            "unsupported_claims": unsupported,
            "within_limit": word_limit_ok(body, word_limit),
            "word_count": len(re.findall(r"[A-Za-z0-9']+", body)),
        }


def _clean(text: str) -> str:
    text = re.sub(r"^\s*```[a-z]*\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass(slots=True)
class EditEssayDraft:
    """Student edits a draft: rescored deterministically; clears `requires_manual`."""

    deps: EssayDeps

    async def execute(self, user_id: uuid.UUID, draft_id: uuid.UUID, body: str) -> EssayDraft:
        async with self.deps.uow_factory(user_id) as uow:
            draft = await uow.essay_drafts.get(user_id, draft_id)
            fingerprint = await uow.fingerprints.latest(user_id)
        if draft is None:
            raise NotFoundError("draft not found")
        if not body.strip():
            raise ValidationError("essay text is empty")
        draft.body = body.strip()
        draft.status = EssayStatus.USER_EDITED
        draft.requires_manual = False
        draft.critique = {
            **draft.critique,
            "cliches": find_cliches(draft.body),
            "within_limit": word_limit_ok(draft.body, draft.word_limit),
            "edited_by_user": True,
        }
        if fingerprint is not None:
            scores = SynthesizeEssay(self.deps).score(
                draft.body, fingerprint.feature_vector, fingerprint.metrics
            )
            draft.voice_match_score, draft.burstiness_score = (
                scores["voice_match"],
                scores["burstiness"],
            )
        draft.updated_at = self.deps.clock.now()
        async with self.deps.uow_factory(user_id) as uow:
            await uow.essay_drafts.update(draft)
            await uow.commit()
        return draft


@dataclass(slots=True)
class ListEssayDrafts:
    uow_factory: IUnitOfWorkFactory

    async def execute(
        self, user_id: uuid.UUID, application_id: uuid.UUID | None = None
    ) -> list[EssayDraft]:
        async with self.uow_factory(user_id) as uow:
            return await uow.essay_drafts.list(user_id, application_id)
