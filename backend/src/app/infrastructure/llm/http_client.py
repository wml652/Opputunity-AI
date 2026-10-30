"""Minimal OpenAI-compatible REST client over httpx (`/chat/completions`, `/models`,
`/embeddings`). Kept in-house so the wire contract, retries, and error redaction are explicit and
mockable with respx, independent of SDK transport changes."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from typing import Any

import httpx

from app.domain.llm.provider import ConnectionErrorCode, LLMProviderError
from app.infrastructure.llm.rate_limiter import SlidingWindowRateLimiter
from app.infrastructure.logging import redact_text

_MAX_ERROR_CHARS = 300
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LLMHttpError(LLMProviderError):
    code: ConnectionErrorCode

    def __init__(self, code: ConnectionErrorCode, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _code_for_status(status: int) -> ConnectionErrorCode:
    if status == 401:
        return ConnectionErrorCode.UNAUTHORIZED
    if status == 403:
        return ConnectionErrorCode.FORBIDDEN
    if status in (404, 410):  # 410 Gone = model retired by the provider
        return ConnectionErrorCode.MODEL_NOT_FOUND
    if status == 429:
        return ConnectionErrorCode.RATE_LIMITED
    if status >= 500:
        return ConnectionErrorCode.UPSTREAM
    return ConnectionErrorCode.UNKNOWN


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, Mapping):
        err = body.get("error", body.get("detail", body))
        if isinstance(err, Mapping):
            return str(err.get("message") or err.get("detail") or err)
        return str(err)
    return str(body)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), 30.0))
    except ValueError:
        return None


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        http_client: httpx.AsyncClient,
        timeout: float,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self._limiter = limiter
        self._base = base_url.rstrip("/")
        self._http = http_client
        self._timeout = httpx.Timeout(timeout, connect=min(10.0, timeout))
        self._headers = {"accept": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        max_retries: int = 0,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            if self._limiter is not None:
                await self._limiter.acquire()  # proactive RPM pacing (retries included)
            try:
                response = await self._http.request(
                    method,
                    f"{self._base}{path}",
                    json=json,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                error = LLMHttpError(ConnectionErrorCode.TIMEOUT, "request timed out")
                if attempt >= max_retries:
                    raise error from exc
            except httpx.HTTPError as exc:
                error = LLMHttpError(
                    ConnectionErrorCode.NETWORK, redact_text(f"network error: {type(exc).__name__}")
                )
                if attempt >= max_retries:
                    raise error from exc
            else:
                if response.is_success:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise LLMHttpError(
                            ConnectionErrorCode.UPSTREAM,
                            "provider returned non-JSON body",
                            response.status_code,
                        ) from exc
                    if not isinstance(payload, dict):
                        raise LLMHttpError(
                            ConnectionErrorCode.UPSTREAM,
                            "unexpected response shape",
                            response.status_code,
                        )
                    return payload
                message = redact_text(f"HTTP {response.status_code}: {_error_detail(response)}")[
                    :_MAX_ERROR_CHARS
                ]
                error = LLMHttpError(
                    _code_for_status(response.status_code), message, response.status_code
                )
                if response.status_code not in _RETRYABLE_STATUS or attempt >= max_retries:
                    raise error
                delay = _retry_after_seconds(response)
                if delay is not None:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            attempt += 1
            await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))  # noqa: S311
