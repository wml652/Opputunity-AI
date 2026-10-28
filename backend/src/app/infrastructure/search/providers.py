"""Web search adapters behind `ISearchProvider`.

Tavily and Brave need API keys (TAVILY_API_KEY / BRAVE_API_KEY); `DdgsSearchAdapter` needs none.
`ChainedSearchProvider` tries each configured provider in order and falls through on errors or
empty results, so discovery works with zero keys and improves when keys are added."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from app.domain.ports.discovery import ISearchProvider, SearchHit
from app.infrastructure.logging import get_logger, redact_text

log = get_logger(__name__)


class SearchError(Exception):
    pass


class TavilySearchAdapter:
    name = "tavily"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._key = api_key
        self._http = http

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            r = await self._http.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
                headers={"authorization": f"Bearer {self._key}"},
                timeout=20,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchError(redact_text(f"tavily: {exc}")) from exc
        return [
            SearchHit(
                title=str(i.get("title") or ""),
                url=str(i["url"]),
                snippet=str(i.get("content") or "")[:500],
                provider=self.name,
            )
            for i in r.json().get("results", [])
            if i.get("url")
        ]


class BraveSearchAdapter:
    name = "brave"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._key = api_key
        self._http = http

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            r = await self._http.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"x-subscription-token": self._key, "accept": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchError(redact_text(f"brave: {exc}")) from exc
        results = (r.json().get("web") or {}).get("results", [])
        return [
            SearchHit(
                title=str(i.get("title") or ""),
                url=str(i["url"]),
                snippet=str(i.get("description") or "")[:500],
                provider=self.name,
            )
            for i in results
            if i.get("url")
        ]


def _ddgs_text(query: str, max_results: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    return list(DDGS().text(query, max_results=max_results) or [])


class DdgsSearchAdapter:
    """Zero-key fallback (DuckDuckGo et al. via the `ddgs` package); runs in a worker thread."""

    name = "ddgs"

    def __init__(self, runner: Callable[[str, int], list[dict[str, Any]]] = _ddgs_text) -> None:
        self._runner = runner

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            rows = await asyncio.to_thread(self._runner, query, max_results)
        except Exception as exc:
            raise SearchError(f"ddgs: {type(exc).__name__}") from exc
        return [
            SearchHit(
                title=str(r.get("title") or ""),
                url=str(r.get("href") or r.get("url")),
                snippet=str(r.get("body") or "")[:500],
                provider=self.name,
            )
            for r in rows
            if r.get("href") or r.get("url")
        ]


class ChainedSearchProvider:
    name = "chained"

    def __init__(self, providers: Sequence[ISearchProvider]) -> None:
        self._providers = list(providers)

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        for provider in self._providers:
            try:
                hits = await provider.search(query, max_results)
            except SearchError as exc:
                log.warning("search.provider_failed", provider=provider.name, error=str(exc))
                continue
            unique = list({h.url: h for h in hits}.values())
            if unique:
                return unique[:max_results]
        return []


def build_search_chain(
    *, tavily_key: str | None, brave_key: str | None, http: httpx.AsyncClient
) -> ChainedSearchProvider:
    providers: list[ISearchProvider] = []
    if tavily_key:
        providers.append(TavilySearchAdapter(tavily_key, http))
    if brave_key:
        providers.append(BraveSearchAdapter(brave_key, http))
    providers.append(DdgsSearchAdapter())
    return ChainedSearchProvider(providers)
