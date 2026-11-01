from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent.models import AgentWorkflow, AtsType, FieldMapping, TeachStep
from app.infrastructure.db.models import AgentWorkflowRow


def _workflow(r: AgentWorkflowRow) -> AgentWorkflow:
    steps = [s for s in (TeachStep.from_dict(d) for d in r.navigation_steps or []) if s]
    return AgentWorkflow(
        id=r.id,
        user_id=r.user_id,
        domain=r.domain,
        ats_type=AtsType(r.ats_type),
        navigation_steps=steps,
        field_mappings=[FieldMapping.from_dict(d) for d in r.field_mappings or []],
        source_application_id=r.source_application_id,
        times_used=r.times_used,
        success_count=r.success_count,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


class SqlAgentWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def find(
        self, user_id: uuid.UUID, domain: str, ats_type: AtsType | None = None
    ) -> AgentWorkflow | None:
        row = await self._s.scalar(
            select(AgentWorkflowRow)
            .where(AgentWorkflowRow.user_id == user_id, AgentWorkflowRow.domain == domain)
            .order_by(AgentWorkflowRow.updated_at.desc())
            .limit(1)
        )
        if row is None and ats_type is not None and ats_type is not AtsType.GENERIC:
            # Same applicant tracking system on another domain: its field memory still helps.
            row = await self._s.scalar(
                select(AgentWorkflowRow)
                .where(
                    AgentWorkflowRow.user_id == user_id,
                    AgentWorkflowRow.ats_type == ats_type.value,
                )
                .order_by(AgentWorkflowRow.success_count.desc(), AgentWorkflowRow.updated_at.desc())
                .limit(1)
            )
        return _workflow(row) if row is not None else None

    async def upsert(self, workflow: AgentWorkflow) -> None:
        values: dict[str, Any] = {
            "navigation_steps": [s.to_dict() for s in workflow.navigation_steps],
            "field_mappings": [
                {
                    "field_label": m.field_label,
                    "field_name": m.field_name,
                    "field_type": m.field_type,
                    "user_value": m.user_value,
                    "mapped_profile_path": m.mapped_profile_path,
                    "memory_qa_id": m.memory_qa_id,
                }
                for m in workflow.field_mappings
            ],
            "source_application_id": workflow.source_application_id,
            "updated_at": func.now(),
        }
        stmt = pg_insert(AgentWorkflowRow).values(
            id=workflow.id,
            user_id=workflow.user_id,
            domain=workflow.domain,
            ats_type=workflow.ats_type.value,
            **values,
        )
        await self._s.execute(
            stmt.on_conflict_do_update(constraint="uq_agent_workflows_domain", set_=values)
        )

    async def list(self, user_id: uuid.UUID) -> list[AgentWorkflow]:
        rows = await self._s.scalars(
            select(AgentWorkflowRow)
            .where(AgentWorkflowRow.user_id == user_id)
            .order_by(AgentWorkflowRow.updated_at.desc())
        )
        return [_workflow(r) for r in rows]

    async def record_use(self, user_id: uuid.UUID, workflow_id: uuid.UUID, success: bool) -> None:
        await self._s.execute(
            update(AgentWorkflowRow)
            .where(AgentWorkflowRow.user_id == user_id, AgentWorkflowRow.id == workflow_id)
            .values(
                times_used=AgentWorkflowRow.times_used + 1,
                success_count=AgentWorkflowRow.success_count + (1 if success else 0),
            )
        )
