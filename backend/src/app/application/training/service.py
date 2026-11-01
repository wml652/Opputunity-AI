"""The agent's experience store: record trajectories (Teach Mode, autonomous decisions, backfill),
recall the most similar taught DOM states for a new page (DOM-RAG), and export SFT / DPO datasets.

Privacy: every signature and action is PII-tokenized with the student's own values BEFORE it is
embedded or stored; exports are tokenized again with the current profile and every record is
re-checked - a record that still contains a personal value is dropped, never exported."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.application.llm.use_cases import GetEmbeddingProvider
from app.application.profile.use_cases import GetProfile
from app.application.services.field_mapper import match_profile_key
from app.domain.agent.models import workflow_domain
from app.domain.applications.filling import FieldSource
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import ApplicationState
from app.domain.shared.errors import ValidationError
from app.domain.shared.ids import new_uuid7
from app.domain.training.models import (
    Demonstration,
    DomState,
    TrainingStats,
    Trajectory,
    TrajectorySource,
    click_action,
    dpo_records,
    fill_action,
    sft_record,
)
from app.domain.training.privacy import leaks, profile_token, tokenize_pii

RECALL_K = 3
# Only fills that reached the human review gate (or a pending question) are trustworthy examples;
# a run that ended blocked may have typed into the wrong form.
VERIFIED_STATES = (
    ApplicationState.PAUSED_NEEDS_USER_INPUT,
    ApplicationState.AWAITING_FINAL_VERIFICATION,
    ApplicationState.SUBMIT_APPROVED,
    ApplicationState.SUBMITTING,
    ApplicationState.SUBMITTED,
    ApplicationState.SUBMISSION_FAILED,
)
RECALL_SOURCES = (TrajectorySource.TEACH, TrajectorySource.AUTO_SUCCESS, TrajectorySource.BACKFILL)
SOURCE_TOKENS = {
    FieldSource.MEMORY: "{{MEMORY.QA_MATCH}}",
    FieldSource.ESSAY: "{{ESSAY.DRAFT}}",
    FieldSource.DOCUMENT: "{{DOCUMENT.FILE}}",
    FieldSource.USER: "{{USER.ANSWER}}",
}


def tokenize_action(action: dict[str, Any], known: dict[str, str]) -> dict[str, Any]:
    return {k: tokenize_pii(v, known) if isinstance(v, str) else v for k, v in action.items()}


@dataclass(frozen=True, slots=True)
class TrainingDeps:
    uow_factory: IUnitOfWorkFactory
    embeddings: GetEmbeddingProvider
    get_profile: GetProfile
    clock: Clock


@dataclass(slots=True)
class TrajectoryService:
    deps: TrainingDeps

    async def known_values(self, user_id: uuid.UUID) -> dict[str, str]:
        """fact key -> the student's personal value (what must never leave as plain text)."""
        view = await self.deps.get_profile.execute(user_id)
        async with self.deps.uow_factory(user_id) as uow:
            user = await uow.users.get(user_id)
        known: dict[str, str] = {}
        if user and "@opportunity.local" not in user.email:
            known["email"] = user.email
        if view is not None:
            p = view.profile
            parts = p.full_name.split()
            known["full_name"] = p.full_name
            if parts:
                known["first_name"] = parts[0]
            if len(parts) > 1:
                known["last_name"] = parts[-1]
            known |= {k: v for k, v in p.pii.to_dict().items() if v}
        return {k: v for k, v in known.items() if v}

    async def record(
        self,
        user_id: uuid.UUID,
        source: TrajectorySource,
        *,
        url: str,
        signature: str,
        action: dict[str, Any],
        application_id: uuid.UUID | None = None,
        rejected: dict[str, Any] | None = None,
        known: dict[str, str] | None = None,
    ) -> Trajectory:
        known = known if known is not None else await self.known_values(user_id)
        embedder = await self.deps.embeddings.execute(user_id)
        clean = tokenize_pii(signature, known)
        trajectory = Trajectory(
            id=new_uuid7(),
            user_id=user_id,
            source=source,
            domain=workflow_domain(url),
            dom_signature=clean,
            chosen_action=tokenize_action(action, known),
            rejected_action=tokenize_action(rejected, known) if rejected else None,
            application_id=application_id,
            embedding_model=embedder.model_name,
            created_at=self.deps.clock.now(),
        )
        vector = await embedder.embed_query(clean)
        async with self.deps.uow_factory(user_id) as uow:
            await uow.trajectories.add(trajectory, vector)
            await uow.commit()
        return trajectory

    async def recall(
        self, user_id: uuid.UUID, signature: str, known: dict[str, str]
    ) -> list[Demonstration]:
        """The student's most similar taught / successful DOM states on ANY portal."""
        embedder = await self.deps.embeddings.execute(user_id)
        vector = await embedder.embed_query(tokenize_pii(signature, known))
        async with self.deps.uow_factory(user_id) as uow:
            return await uow.trajectories.nearest(
                user_id, vector, embedder.model_name, RECALL_K, RECALL_SOURCES
            )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    from_workflows: int
    from_fields: int


@dataclass(slots=True)
class BackfillTrajectories:
    """Starter training data from past runs: learned workflows (routes + field mappings) and the
    fields the agent filled on earlier applications that reached review (VERIFIED_STATES).
    Idempotent (replaces earlier backfill)."""

    service: TrajectoryService

    async def execute(self, user_id: uuid.UUID) -> BackfillResult:
        deps = self.service.deps
        known = await self.service.known_values(user_id)
        async with deps.uow_factory(user_id) as uow:
            await uow.trajectories.delete_source(user_id, TrajectorySource.BACKFILL)
            workflows = await uow.agent_workflows.list(user_id)
            pairs = await uow.app_details.list_by_states(user_id, list(VERIFIED_STATES))
            await uow.commit()
        from_workflows = from_fields = 0
        for wf in workflows:
            buttons = [s.locator.name for s in wf.navigation_steps if s.locator.name]
            for step in wf.navigation_steps:
                if not step.locator.name:
                    continue
                state = DomState.of(step.url or f"https://{wf.domain}/", buttons=buttons)
                await self.service.record(
                    user_id,
                    TrajectorySource.BACKFILL,
                    url=step.url or f"https://{wf.domain}/",
                    signature=state.signature(),
                    action=click_action(step.locator.name, step.locator.role, step.locator.css),
                    known=known,
                )
                from_workflows += 1
            labels = [m.field_label for m in wf.field_mappings if m.field_label]
            for m in wf.field_mappings:
                token = (
                    profile_token(m.mapped_profile_path)
                    if m.mapped_profile_path
                    else "{{MEMORY.QA_MATCH}}"
                    if m.memory_qa_id
                    else None
                )
                if not token or not m.field_label:
                    continue
                url = f"https://{wf.domain}/"
                await self.service.record(
                    user_id,
                    TrajectorySource.BACKFILL,
                    url=url,
                    signature=DomState.of(url, inputs=labels).signature(),
                    action=fill_action(m.field_label, token, m.field_type),
                    known=known,
                )
                from_workflows += 1
        for app, opp in pairs:
            async with deps.uow_factory(user_id) as uow:
                stored = await uow.fields.for_application(user_id, app.id)
            by_step: dict[int, list[Any]] = {}
            for s in stored:
                if s.value is not None and s.field.source is not FieldSource.NONE:
                    by_step.setdefault(s.field.step_index, []).append(s.field)
            url = opp.portal_url or opp.source_url or ""
            for step_fields in by_step.values():
                labels = [f.label for f in step_fields]
                signature = DomState.of(url, title=opp.title, inputs=labels).signature()
                for f in step_fields:
                    if f.source is FieldSource.PROFILE:
                        key = match_profile_key(f.label) or f.field_key
                        token = profile_token(key)
                    else:
                        token = SOURCE_TOKENS.get(f.source, "{{USER.ANSWER}}")
                    await self.service.record(
                        user_id,
                        TrajectorySource.BACKFILL,
                        url=url,
                        signature=signature,
                        action=fill_action(f.label, token, f.field_type),
                        application_id=app.id,
                        known=known,
                    )
                    from_fields += 1
        return BackfillResult(from_workflows, from_fields)


@dataclass(frozen=True, slots=True)
class ExportResult:
    records: list[dict[str, Any]]
    dropped: int


@dataclass(slots=True)
class ExportTrainingData:
    service: TrajectoryService

    async def execute(self, user_id: uuid.UUID, fmt: str) -> ExportResult:
        if fmt not in ("sft", "dpo"):
            raise ValidationError("format must be sft or dpo")
        known = await self.service.known_values(user_id)
        async with self.service.deps.uow_factory(user_id) as uow:
            trajectories = await uow.trajectories.all(user_id)
        # Re-tokenize with the CURRENT profile (values may have changed since recording).
        for t in trajectories:
            t.dom_signature = tokenize_pii(t.dom_signature, known)
            t.chosen_action = tokenize_action(t.chosen_action, known)
            t.chosen_action.pop("tier", None)
            if t.rejected_action:
                t.rejected_action = tokenize_action(t.rejected_action, known)
                t.rejected_action.pop("tier", None)
        raw = [sft_record(t) for t in trajectories] if fmt == "sft" else dpo_records(trajectories)
        safe = [r for r in raw if not leaks(json.dumps(r, ensure_ascii=False), known)]
        return ExportResult(safe, len(raw) - len(safe))


@dataclass(frozen=True, slots=True)
class TrainingOverview:
    stats: TrainingStats
    recent: Sequence[Trajectory]


@dataclass(slots=True)
class GetTrainingOverview:
    service: TrajectoryService

    async def execute(self, user_id: uuid.UUID, limit: int = 50) -> TrainingOverview:
        async with self.service.deps.uow_factory(user_id) as uow:
            stats = await uow.trajectories.stats(user_id)
            recent = await uow.trajectories.recent(user_id, limit)
        return TrainingOverview(stats, recent)
