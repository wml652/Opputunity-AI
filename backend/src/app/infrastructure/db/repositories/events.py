from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.events.models import NewEvent, PipelineEvent
from app.domain.shared.enums import Actor
from app.domain.shared.ids import new_uuid7
from app.infrastructure.db.models import AuditLogRow, PipelineEventRow


def _event(row: PipelineEventRow) -> PipelineEvent:
    return PipelineEvent(
        id=row.id,
        seq=row.seq,
        user_id=row.user_id,
        type=row.type,
        payload=dict(row.payload),
        campaign_id=row.campaign_id,
        application_id=row.application_id,
        created_at=row.created_at,
    )


class SqlEventOutbox:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(self, event: NewEvent) -> PipelineEvent:
        row = await self._s.scalar(
            insert(PipelineEventRow)
            .values(
                id=new_uuid7(),
                user_id=event.user_id,
                type=event.type,
                payload=event.payload,
                campaign_id=event.campaign_id,
                application_id=event.application_id,
            )
            .returning(PipelineEventRow)
        )
        assert row is not None
        return _event(row)

    async def list_since(
        self, user_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[PipelineEvent]:
        rows = await self._s.scalars(
            select(PipelineEventRow)
            .where(PipelineEventRow.user_id == user_id, PipelineEventRow.seq > after_seq)
            .order_by(PipelineEventRow.seq)
            .limit(limit)
        )
        return [_event(r) for r in rows]

    async def claim_unpublished(self, limit: int) -> list[PipelineEvent]:
        rows = await self._s.scalars(
            select(PipelineEventRow)
            .where(PipelineEventRow.published_at.is_(None))
            .order_by(PipelineEventRow.seq)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_event(r) for r in rows]

    async def mark_published(self, event_ids: Sequence[uuid.UUID], at: datetime) -> None:
        if not event_ids:
            return
        await self._s.execute(
            update(PipelineEventRow)
            .where(PipelineEventRow.id.in_(list(event_ids)))
            .values(published_at=at)
        )


class SqlAuditLog:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        user_id: uuid.UUID,
        actor: Actor,
        action: str,
        entity: str,
        entity_id: uuid.UUID | None = None,
        ip: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._s.add(
            AuditLogRow(
                id=new_uuid7(),
                user_id=user_id,
                actor=actor,
                action=action,
                entity=entity,
                entity_id=entity_id,
                ip=ip,
                metadata_=metadata or {},
            )
        )
        await self._s.flush()
