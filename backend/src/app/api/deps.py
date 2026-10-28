from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request

from app.api.container import ApiContainer
from app.api.cookies import ACCESS_COOKIE
from app.domain.shared.errors import AuthenticationError


def get_container(request: Request) -> ApiContainer:
    container: ApiContainer = request.app.state.container
    return container


Container = Annotated[ApiContainer, Depends(get_container)]


async def current_user_id(request: Request, container: Container) -> uuid.UUID:
    if container.config.dev_auth_bypass:
        cached: uuid.UUID | None = getattr(request.app.state, "dev_user_id", None)
        if cached is None:
            cached = await container.ensure_dev_user.execute()
            request.app.state.dev_user_id = cached
        return cached
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token:
        token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise AuthenticationError("not authenticated")
    return container.access_tokens.verify(token).user_id


CurrentUserId = Annotated[uuid.UUID, Depends(current_user_id)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
