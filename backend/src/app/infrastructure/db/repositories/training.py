from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.llm.embedding import EMBEDDING_DIM
from app.domain.training.models import Demonstration, TrainingStats, Trajectory, TrajectorySource
from app.infrastructure.db.repositories.vault import vector_literal

_COLUMNS = (
    "id, user_id, application_id, source, domain, dom_signature, chosen_action, "
    "rejected_action, embedding_model, created_at"
)


def _load(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def _trajectory(r: Any) -> Trajectory:
    return Trajectory(
        id=r.id,
        user_id=r.user_id,
        source=TrajectorySource(r.source),
        domain=r.domain,
        dom_signature=r.dom_signature,
        chosen_action=_load(r.chosen_action) or {},
        rejected_action=_load(r.rejected_action),
        application_id=r.application_id,
        embedding_model=r.embedding_model,
        created_at=r.created_at,
    )


class SqlTrajectoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, trajectory: Trajectory, vector: Sequence[float]) -> None:
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(f"expected a {EMBEDDING_DIM}-dim vector, got {len(vector)}")
        await self._s.execute(
            text(
                "INSERT INTO agent_trajectories (id, user_id, application_id, source, domain, "
                "dom_signature, chosen_action, rejected_action, embedding_model, dom_embedding) "
                "VALUES (:id, :u, :a, :s, :d, :sig, CAST(:c AS jsonb), CAST(:r AS jsonb), :m, "
                "CAST(CAST(:v AS text) AS vector))"
            ),
            {
                "id": trajectory.id,
                "u": trajectory.user_id,
                "a": trajectory.application_id,
                "s": trajectory.source.value,
                "d": trajectory.domain[:255],
                "sig": trajectory.dom_signature,
                "c": json.dumps(trajectory.chosen_action),
                "r": json.dumps(trajectory.rejected_action) if trajectory.rejected_action else None,
                "m": trajectory.embedding_model,
                "v": vector_literal(vector),
            },
        )

    async def nearest(
        self,
        user_id: uuid.UUID,
        vector: Sequence[float],
        embedding_model: str,
        k: int,
        sources: Sequence[TrajectorySource],
    ) -> builtins.list[Demonstration]:
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH mine AS MATERIALIZED (
                        SELECT domain, dom_signature, chosen_action, dom_embedding
                        FROM agent_trajectories
                        WHERE user_id = :u AND embedding_model = :m AND source = ANY(:s)
                    )
                    SELECT domain, dom_signature, chosen_action,
                           1 - (dom_embedding <=> CAST(CAST(:v AS text) AS vector)) AS sim
                    FROM mine
                    ORDER BY dom_embedding <=> CAST(CAST(:v AS text) AS vector) LIMIT :k
                    """
                ),
                {
                    "u": user_id,
                    "m": embedding_model,
                    "s": [s.value for s in sources],
                    "v": vector_literal(vector),
                    "k": k,
                },
            )
        ).all()
        return [
            Demonstration(r.domain, r.dom_signature, _load(r.chosen_action) or {}, float(r.sim))
            for r in rows
        ]

    async def failures_for(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> builtins.list[Trajectory]:
        rows = await self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM agent_trajectories WHERE user_id = :u "  # noqa: S608 - constant column list
                "AND application_id = :a AND source = 'auto_failure' ORDER BY created_at"
            ),
            {"u": user_id, "a": application_id},
        )
        return [_trajectory(r) for r in rows]

    async def set_rejected(
        self, user_id: uuid.UUID, trajectory_id: uuid.UUID, rejected: dict[str, Any]
    ) -> None:
        await self._s.execute(
            text(
                "UPDATE agent_trajectories SET rejected_action = CAST(:r AS jsonb) "
                "WHERE user_id = :u AND id = :id"
            ),
            {"u": user_id, "id": trajectory_id, "r": json.dumps(rejected)},
        )

    async def recent(self, user_id: uuid.UUID, limit: int) -> builtins.list[Trajectory]:
        rows = await self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM agent_trajectories WHERE user_id = :u "  # noqa: S608 - constant column list
                "ORDER BY created_at DESC LIMIT :n"
            ),
            {"u": user_id, "n": limit},
        )
        return [_trajectory(r) for r in rows]

    async def all(self, user_id: uuid.UUID) -> builtins.list[Trajectory]:
        rows = await self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM agent_trajectories WHERE user_id = :u ORDER BY created_at"  # noqa: S608 - constant column list
            ),
            {"u": user_id},
        )
        return [_trajectory(r) for r in rows]

    async def stats(self, user_id: uuid.UUID) -> TrainingStats:
        rows = (
            await self._s.execute(
                text(
                    "SELECT source, count(*) AS n, "
                    "count(*) FILTER (WHERE rejected_action IS NOT NULL) AS pairs, "
                    "count(*) FILTER (WHERE chosen_action->>'tier' = 'rag') AS rag, "
                    "count(*) FILTER (WHERE chosen_action->>'tier' = 'llm') AS llm "
                    "FROM agent_trajectories WHERE user_id = :u GROUP BY source"
                ),
                {"u": user_id},
            )
        ).all()
        return TrainingStats(
            total=sum(int(r.n) for r in rows),
            by_source={r.source: int(r.n) for r in rows},
            dpo_pairs=sum(int(r.pairs) for r in rows),
            rag_decisions=sum(int(r.rag) for r in rows),
            llm_decisions=sum(int(r.llm) for r in rows),
        )

    async def delete_source(self, user_id: uuid.UUID, source: TrajectorySource) -> int:
        result = await self._s.execute(
            text("DELETE FROM agent_trajectories WHERE user_id = :u AND source = :s"),
            {"u": user_id, "s": source.value},
        )
        return int(getattr(result, "rowcount", 0) or 0)
