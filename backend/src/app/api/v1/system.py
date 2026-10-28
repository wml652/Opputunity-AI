from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import Container, CurrentUserId
from app.api.v1.schemas import WorkerOut, WorkerStatusOut

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system/workers", response_model=WorkerStatusOut)
async def workers(_user: CurrentUserId, container: Container) -> WorkerStatusOut:
    status = await container.worker_status.execute()
    return WorkerStatusOut(
        active=status.active, workers=[WorkerOut.model_validate(w) for w in status.workers]
    )
