"""Agent Brain & Training: trajectory telemetry, backfill, PII-tokenized SFT / DPO exports and the
cloud QLoRA training kit (Colab / Kaggle / Azure notebook + CLI script)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel

from app.api.deps import Container, CurrentUserId
from app.domain.shared.errors import NotFoundError

router = APIRouter(tags=["training"])


class StatsOut(BaseModel):
    total: int
    by_source: dict[str, int]
    sft_records: int
    dpo_pairs: int
    rag_decisions: int
    llm_decisions: int
    rag_hit_rate: float


class TrajectoryOut(BaseModel):
    id: uuid.UUID
    source: str
    domain: str
    dom_signature: str
    chosen_action: dict[str, Any]
    rejected_action: dict[str, Any] | None
    created_at: datetime | None


class OverviewOut(BaseModel):
    stats: StatsOut
    trajectories: list[TrajectoryOut]


class BackfillOut(BaseModel):
    from_workflows: int
    from_fields: int


@router.get("/training/overview", response_model=OverviewOut)
async def overview(
    user_id: CurrentUserId, container: Container, limit: int = Query(default=50, ge=1, le=500)
) -> OverviewOut:
    data = await container.training.overview.execute(user_id, limit)
    s = data.stats
    return OverviewOut(
        stats=StatsOut(
            total=s.total,
            by_source=s.by_source,
            sft_records=s.total,
            dpo_pairs=s.dpo_pairs,
            rag_decisions=s.rag_decisions,
            llm_decisions=s.llm_decisions,
            rag_hit_rate=s.rag_hit_rate,
        ),
        trajectories=[
            TrajectoryOut(
                id=t.id,
                source=t.source.value,
                domain=t.domain,
                dom_signature=t.dom_signature,
                chosen_action=t.chosen_action,
                rejected_action=t.rejected_action,
                created_at=t.created_at,
            )
            for t in data.recent
        ],
    )


@router.post("/training/backfill", response_model=BackfillOut)
async def backfill(user_id: CurrentUserId, container: Container) -> BackfillOut:
    """Harvest starter trajectories from learned workflows and past runs (idempotent)."""
    result = await container.training.backfill.execute(user_id)
    return BackfillOut(from_workflows=result.from_workflows, from_fields=result.from_fields)


@router.get("/training/export")
async def export(
    user_id: CurrentUserId,
    container: Container,
    format: Literal["sft", "dpo"] = Query(default="sft"),
) -> Response:
    """JSONL download. Every record is PII-tokenized ({{PROFILE.*}}) and re-checked; a record
    that still contains a personal value is dropped (count in X-Dropped-Records)."""
    result = await container.training.export.execute(user_id, format)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in result.records)
    return Response(
        content=(body + "\n") if body else "",
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{format}.jsonl"',
            "X-Records": str(len(result.records)),
            "X-Dropped-Records": str(result.dropped),
            "Cache-Control": "no-store",
        },
    )


@router.get("/training/kit", status_code=status.HTTP_200_OK)
async def training_kit(
    user_id: CurrentUserId,
    container: Container,
    file: Literal["notebook", "script"] = Query(default="notebook"),
) -> Response:
    """The cloud QLoRA training kit shipped with the app (repo `training/`)."""
    del user_id
    found = container.training.kit(file)
    if found is None:
        raise NotFoundError("the training kit is not available in this deployment")
    name, content = found
    media = "application/x-ipynb+json" if name.endswith(".ipynb") else "text/x-python"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
