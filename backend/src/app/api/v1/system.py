from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import Container, CurrentUserId
from app.api.v1.schemas import WorkerOut, WorkerStatusOut

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: Container) -> JSONResponse:
    """Readiness for supervisors / load balancers: 503 while the database or Redis is down."""
    result = await container.readiness.execute()
    body: dict[str, Any] = {
        "status": "ready" if result.ready else "not_ready",
        "checks": result.checks,
        "free_memory_mb": result.free_memory_mb,
    }
    return JSONResponse(body, status_code=200 if result.ready else 503)


@router.get("/system/workers", response_model=WorkerStatusOut)
async def workers(_user: CurrentUserId, container: Container) -> WorkerStatusOut:
    status = await container.worker_status.execute()
    return WorkerStatusOut(
        active=status.active, workers=[WorkerOut.model_validate(w) for w in status.workers]
    )
