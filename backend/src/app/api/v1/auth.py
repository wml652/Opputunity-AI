from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.cookies import REFRESH_COOKIE, clear_session_cookies, set_session_cookies
from app.api.deps import Container, CurrentUserId, client_ip
from app.api.v1.schemas import LoginRequest, RegisterRequest, SessionOut, UserOut
from app.domain.identity.models import IssuedSession
from app.domain.shared.errors import AuthenticationError

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_response(
    response: Response, session: IssuedSession, container: Container
) -> SessionOut:
    csrf = set_session_cookies(response, session, container.config)
    response.headers["cache-control"] = "no-store"
    return SessionOut(
        user=UserOut.of(session.user), access_expires_at=session.access_expires_at, csrf_token=csrf
    )


@router.post("/register", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, response: Response, container: Container) -> SessionOut:
    session = await container.register_user.execute(
        email=str(body.email),
        password=body.password.get_secret_value(),
        display_name=body.display_name,
    )
    return _session_response(response, session, container)


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginRequest, request: Request, response: Response, container: Container
) -> SessionOut:
    session = await container.login_user.execute(
        email=body.email, password=body.password.get_secret_value(), ip=client_ip(request)
    )
    return _session_response(response, session, container)


@router.post("/refresh", response_model=SessionOut)
async def refresh(request: Request, response: Response, container: Container) -> SessionOut:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise AuthenticationError("no refresh token")
    session = await container.refresh_session.execute(raw_refresh_token=raw, ip=client_ip(request))
    return _session_response(response, session, container)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, container: Container) -> Response:
    await container.logout.execute(raw_refresh_token=request.cookies.get(REFRESH_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, container.config)
    return response


@router.get("/me", response_model=UserOut)
async def me(user_id: CurrentUserId, container: Container) -> UserOut:
    return UserOut.of(await container.get_current_user.execute(user_id))
