"""Pure-ASGI middleware (safe for streaming/SSE responses)."""

from __future__ import annotations

import hmac
import uuid
from http.cookies import SimpleCookie

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.cookies import ACCESS_COOKIE, CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# No session exists yet on these, so there is nothing to forge.
CSRF_EXEMPT_PATHS = frozenset({"/api/v1/auth/login", "/api/v1/auth/register"})

_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-site",
    "x-permitted-cross-domain-policies": "none",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
# The API serves JSON, images and SSE only: nothing may execute or be framed. (The interactive
# docs, dev only, need their own scripts and are exempt.)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HSTS = "max-age=63072000; includeSubDomains"


class RequestContextMiddleware:
    """Adds `X-Request-ID` and security headers (CSP, framing, sniffing, permissions, HSTS when
    served over HTTPS) to every response."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if 0 < len(incoming) <= 64 else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        is_docs = str(scope.get("path", "")).startswith(("/api/docs", "/api/openapi"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if not is_docs:
                    headers.setdefault("content-security-policy", API_CSP)
                if self.hsts:
                    headers.setdefault("strict-transport-security", HSTS)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CsrfMiddleware:
    """Double-submit CSRF check for cookie-authenticated, state-changing requests. Requests that
    authenticate with an `Authorization: Bearer` header (API clients) are not CSRF-prone."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if (
            not path.startswith("/api/")
            or path in CSRF_EXEMPT_PATHS
            or path.startswith("/api/v1/webhooks/")
        ):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("authorization", "").lower().startswith("bearer "):
            await self.app(scope, receive, send)
            return
        cookies: SimpleCookie = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001 - malformed cookie header = no cookies
            cookies = SimpleCookie()
        has_session = ACCESS_COOKIE in cookies or REFRESH_COOKIE in cookies
        if has_session:
            cookie_token = cookies[CSRF_COOKIE].value if CSRF_COOKIE in cookies else ""
            header_token = headers.get(CSRF_HEADER, "")
            if not cookie_token or not hmac.compare_digest(cookie_token, header_token):
                response = JSONResponse(
                    {"error": {"code": "csrf_failed", "message": "CSRF token missing or invalid"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
