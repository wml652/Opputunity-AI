"""Ports for the learning agent: the Teach Mode recorder, the workflow store and the LLM
navigator."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.agent.models import AgentWorkflow, AtsType, NavDecision, TeachStep
from app.domain.training.models import Demonstration

# (page snapshot text, goal) -> the next action, decided by the user's own model.
Navigator = Callable[[str, str], Awaitable[NavDecision]]
# DOM-RAG: page signature -> the most similar taught DOM states (any portal), best first.
DomRecall = Callable[[str], Awaitable[list[Demonstration]]]
# Every agent decision (page signature, action incl. "tier" and "url") for the trajectory store.
DecisionSink = Callable[[str, dict[str, Any]], None]
StepSink = Callable[[TeachStep], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TeachResult:
    steps: list[TeachStep]
    storage_state: str | None
    final_url: str
    final_text: str
    screenshot_png: bytes | None = None
    visited_urls: list[str] = field(default_factory=list)


class ITeachRecorder(Protocol):
    async def record(
        self,
        url: str,
        *,
        storage_state: str | None,
        on_step: StepSink,
        timeout_s: float,
        stop: asyncio.Event,
    ) -> TeachResult:
        """Open a VISIBLE browser at `url`, record what the user does (clicks, navigation, field
        values - never passwords) until they finish, close the window or time out."""
        ...


class IAgentWorkflowRepository(Protocol):
    async def find(
        self, user_id: uuid.UUID, domain: str, ats_type: AtsType | None = None
    ) -> AgentWorkflow | None:
        """The workflow for this domain, else one learned on the same ATS."""
        ...

    async def upsert(self, workflow: AgentWorkflow) -> None: ...

    async def list(self, user_id: uuid.UUID) -> list[AgentWorkflow]: ...

    async def record_use(
        self, user_id: uuid.UUID, workflow_id: uuid.UUID, success: bool
    ) -> None: ...
