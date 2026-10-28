from __future__ import annotations

import secrets

from fastapi import Response

from app.api.container import ApiConfig
from app.domain.identity.models import IssuedSession

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"

ACCESS_PATH = "/api"
REFRESH_PATH = "/api/v1/auth"


def set_session_cookies(response: Response, session: IssuedSession, config: ApiConfig) -> str:
    csrf = secrets.token_urlsafe(32)
    common = {"secure": config.cookie_secure, "domain": config.cookie_domain, "samesite": "lax"}
    response.set_cookie(
        ACCESS_COOKIE,
        session.access_token,
        max_age=config.access_ttl_seconds,
        path=ACCESS_PATH,
        httponly=True,
        **common,  # type: ignore[arg-type]
    )
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=config.refresh_ttl_seconds,
        path=REFRESH_PATH,
        httponly=True,
        **common,  # type: ignore[arg-type]
    )
    # Readable by JS on purpose: the double-submit value echoed in the X-CSRF-Token header.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=config.refresh_ttl_seconds,
        path="/",
        httponly=False,
        **common,  # type: ignore[arg-type]
    )
    return csrf


def clear_session_cookies(response: Response, config: ApiConfig) -> None:
    for name, path in (
        (ACCESS_COOKIE, ACCESS_PATH),
        (REFRESH_COOKIE, REFRESH_PATH),
        (CSRF_COOKIE, "/"),
    ):
        response.delete_cookie(
            name,
            path=path,
            domain=config.cookie_domain,
            secure=config.cookie_secure,
            samesite="lax",
        )
