"""Universal OpenAI-compatible chat adapter (NVIDIA NIM, OpenRouter, any custom gateway)."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import httpx

from app.domain.llm.provider import HandshakeResult
from app.domain.ports.llm import ChatMessage, Completion
from app.infrastructure.llm.http_client import LLMHttpError, OpenAICompatibleClient
from app.infrastructure.llm.multimodal import OpenAICompatibleReranker, OpenAICompatibleVision
from app.infrastructure.llm.rate_limiter import RateLimiterRegistry, SlidingWindowRateLimiter
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


REASONING_MAX_TOKENS = 8_192
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def visible_answer(content: str, reasoning: str = "") -> str:
    """The answer without chain-of-thought: strips <think>...</think> blocks (and an unclosed
    leading <think>); if the model put everything in `reasoning_content` and left `content`
    empty, fall back to a JSON object found there."""
    text = _THINK.sub("", content)
    if "<think>" in text.lower():
        text = text[: text.lower().index("<think>")]
    text = text.strip()
    if text or not reasoning:
        return text
    source = _THINK.sub("", reasoning)
    start, end = source.rfind("{"), source.rfind("}")
    if start >= 0 and end > start:
        # The last complete JSON object in the reasoning is the model's intended answer.
        depth, i = 0, end
        while i >= 0:
            depth += source[i] == "}"
            depth -= source[i] == "{"
            if depth == 0:
                return source[i : end + 1]
            i -= 1
    return ""


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.AsyncClient,
        timeout: float,
        max_retries: int = 2,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._max_retries = max_retries
        self._client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            timeout=timeout,
            limiter=limiter,
        )

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        headroom_retry: bool = False,
    ) -> Completion:
        body = await self._client.request(
            "POST",
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            max_retries=self._max_retries,
        )
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        usage = body.get("usage") or {}
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        text = visible_answer(str(message.get("content") or ""), str(reasoning or ""))
        if not text and choice.get("finish_reason") == "length" and not headroom_retry:
            # Reasoning ("thinking") models spend the budget on reasoning first: one retry with
            # more headroom instead of an empty answer.
            return await self.complete(
                messages,
                max_tokens=min(max(max_tokens * 4, 1024), REASONING_MAX_TOKENS),
                temperature=temperature,
                headroom_retry=True,
            )
        return Completion(
            text=text,
            model=str(body.get("model") or self._model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            reasoning=str(reasoning) if reasoning else None,
            finish_reason=choice.get("finish_reason"),
        )

    async def list_models(self) -> list[str]:
        body = await self._client.request("GET", "/models")
        return [str(m["id"]) for m in body.get("data", []) if isinstance(m, dict) and "id" in m]

    async def handshake(self) -> HandshakeResult:
        """One honest round trip (no retries) timed with `perf_counter`, then a `/models` probe
        (optional on some gateways, so its failure does not fail the handshake)."""
        started = time.perf_counter()
        try:
            await self._client.request(
                "POST",
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            )
        except LLMHttpError as exc:
            log.warning(
                "llm.handshake_failed",
                base_url=self._base_url,
                model=self._model,
                error_code=exc.code.value,
                error=exc.message,
            )
            return HandshakeResult(
                ok=False,
                latency_ms=None,
                model=self._model,
                model_listed=None,
                error_code=exc.code,
                error_message=exc.message,
            )
        latency_ms = round((time.perf_counter() - started) * 1000)
        model_listed: bool | None
        try:
            model_listed = self._model in await self.list_models()
        except LLMHttpError as exc:
            log.info("llm.models_probe_unavailable", base_url=self._base_url, error=exc.message)
            model_listed = None
        log.info(
            "llm.handshake_ok",
            base_url=self._base_url,
            model=self._model,
            latency_ms=latency_ms,
            model_listed=model_listed,
        )
        return HandshakeResult(
            ok=True,
            latency_ms=latency_ms,
            model=self._model,
            model_listed=model_listed,
            error_code=None,
            error_message=None,
        )


class OpenAICompatibleProviderFactory:
    """Shares one pooled httpx client across per-request provider instances. Redirects are not
    followed, so a validated host cannot bounce requests to an internal address."""

    def __init__(
        self,
        *,
        timeout: float,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 1,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self.limiters = RateLimiterRegistry()
        self._http = http_client or httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    @property
    def http_client(self) -> httpx.AsyncClient:
        return self._http

    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            http_client=self._http,
            timeout=self._timeout,
            max_retries=self._max_retries,
            limiter=self.limiters.get(base_url, api_key, rpm_limit),
        )

    def create_rerank(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> OpenAICompatibleReranker:
        return OpenAICompatibleReranker(
            OpenAICompatibleClient(
                base_url=base_url,
                api_key=api_key,
                http_client=self._http,
                timeout=min(self._timeout, 60.0),
                limiter=self.limiters.get(base_url, api_key, rpm_limit),
            ),
            model,
        )

    def create_vision(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> OpenAICompatibleVision:
        return OpenAICompatibleVision(
            OpenAICompatibleClient(
                base_url=base_url,
                api_key=api_key,
                http_client=self._http,
                timeout=self._timeout,
                limiter=self.limiters.get(base_url, api_key, rpm_limit),
            ),
            model,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
