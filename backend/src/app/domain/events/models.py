from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class EventType:
    LLM_STATUS = "llm.status"
    LLM_CONFIG_CHANGED = "llm.config_changed"
    WORKER_HEARTBEAT = "system.worker_heartbeat"


@dataclass(frozen=True, slots=True)
class NewEvent:
    """An event to append to the transactional outbox."""

    user_id: uuid.UUID
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    campaign_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    id: uuid.UUID
    seq: int
    user_id: uuid.UUID
    type: str
    payload: dict[str, Any]
    campaign_id: uuid.UUID | None
    application_id: uuid.UUID | None
    created_at: datetime

    def to_wire(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "payload": self.payload,
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "application_id": str(self.application_id) if self.application_id else None,
            "created_at": self.created_at.isoformat(),
        }
