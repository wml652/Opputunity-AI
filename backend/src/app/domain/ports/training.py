"""Port for the agent's trajectory store (DOM-RAG memory + training data)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from app.domain.training.models import Demonstration, TrainingStats, Trajectory, TrajectorySource


class ITrajectoryRepository(Protocol):
    async def add(self, trajectory: Trajectory, vector: Sequence[float]) -> None: ...

    async def nearest(
        self,
        user_id: uuid.UUID,
        vector: Sequence[float],
        embedding_model: str,
        k: int,
        sources: Sequence[TrajectorySource],
    ) -> list[Demonstration]:
        """Most similar DOM states (cosine), same embedding model only, best first."""
        ...

    async def failures_for(self, user_id: uuid.UUID, application_id: uuid.UUID) -> list[Trajectory]:
        """Autonomous decisions of a run on this application that ended blocked."""
        ...

    async def set_rejected(
        self, user_id: uuid.UUID, trajectory_id: uuid.UUID, rejected: dict[str, Any]
    ) -> None: ...

    async def recent(self, user_id: uuid.UUID, limit: int) -> list[Trajectory]: ...

    async def all(self, user_id: uuid.UUID) -> list[Trajectory]: ...

    async def stats(self, user_id: uuid.UUID) -> TrainingStats: ...

    async def delete_source(self, user_id: uuid.UUID, source: TrajectorySource) -> int: ...
