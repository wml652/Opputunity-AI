"""Browser sessions (user-solved bot challenges, opt-in LinkedIn) and per-user search API keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import Container, CurrentUserId
from app.application.discovery.sessions import (
    ChallengedDomain,
    SearchCredentialView,
    SessionView,
)

router = APIRouter(tags=["sessions"])


class SessionOut(BaseModel):
    id: uuid.UUID
    domain: str
    kind: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    tos_acknowledged_at: datetime | None
    daily_action_cap: int | None
    actions_today: int

    @staticmethod
    def of(v: SessionView) -> SessionOut:
        return SessionOut(
            id=v.id,
            domain=v.domain,
            kind=v.kind,
            created_at=v.created_at,
            last_used_at=v.last_used_at,
            expires_at=v.expires_at,
            tos_acknowledged_at=v.tos_acknowledged_at,
            daily_action_cap=v.daily_action_cap,
            actions_today=v.actions_today,
        )


class ChallengedOut(BaseModel):
    domain: str
    vendor: str
    url: str | None
    campaign_id: str | None
    solved: bool

    @staticmethod
    def of(c: ChallengedDomain) -> ChallengedOut:
        return ChallengedOut(
            domain=c.domain, vendor=c.vendor, url=c.url, campaign_id=c.campaign_id, solved=c.solved
        )


class SessionsOut(BaseModel):
    sessions: list[SessionOut]
    challenged: list[ChallengedOut]
    headful_available: bool


class CaptureIn(BaseModel):
    url: str = Field(default="", max_length=2000)
    kind: str = Field(default="CHALLENGE", max_length=20)
    tos_acknowledged: bool = False
    daily_action_cap: int | None = Field(default=None, ge=1, le=100)


class CaptureOut(BaseModel):
    domain: str
    message: str


class SearchKeyIn(BaseModel):
    api_key: SecretStr = Field(min_length=8, max_length=500)


class SearchKeyOut(BaseModel):
    provider: str
    key_last4: str
    updated_at: datetime

    @staticmethod
    def of(v: SearchCredentialView) -> SearchKeyOut:
        return SearchKeyOut(provider=v.provider, key_last4=v.key_last4, updated_at=v.updated_at)


@router.get("/browser-sessions", response_model=SessionsOut)
async def list_sessions(user_id: CurrentUserId, container: Container) -> SessionsOut:
    sessions, challenged = await container.sessions.list_sessions.execute(user_id)
    return SessionsOut(
        sessions=[SessionOut.of(s) for s in sessions],
        challenged=[ChallengedOut.of(c) for c in challenged],
        headful_available=container.sessions.headful_available,
    )


@router.post(
    "/browser-sessions/capture", response_model=CaptureOut, status_code=status.HTTP_202_ACCEPTED
)
async def capture_session(
    body: CaptureIn, user_id: CurrentUserId, container: Container
) -> CaptureOut:
    domain = await container.sessions.capture.execute(
        user_id,
        body.url,
        body.kind,
        tos_acknowledged=body.tos_acknowledged,
        daily_action_cap=body.daily_action_cap,
    )
    return CaptureOut(
        domain=domain,
        message="A browser window opened on this computer. Complete the check or sign in there; "
        "the window closes by itself once the site is usable.",
    )


@router.delete("/browser-sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    await container.sessions.delete_session.execute(user_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/search/credentials", response_model=list[SearchKeyOut])
async def list_search_keys(user_id: CurrentUserId, container: Container) -> list[SearchKeyOut]:
    return [SearchKeyOut.of(v) for v in await container.sessions.list_search.execute(user_id)]


@router.put("/search/credentials/{provider}", response_model=SearchKeyOut)
async def save_search_key(
    provider: str, body: SearchKeyIn, user_id: CurrentUserId, container: Container
) -> SearchKeyOut:
    view = await container.sessions.save_search.execute(
        user_id, provider, body.api_key.get_secret_value()
    )
    return SearchKeyOut.of(view)


@router.delete("/search/credentials/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_search_key(
    provider: str, user_id: CurrentUserId, container: Container
) -> Response:
    await container.sessions.delete_search.execute(user_id, provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
