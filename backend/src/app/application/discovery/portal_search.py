"""Official portal search: the last resort when an opportunity only has an aggregator page (with
no outbound link) or its portal is dead. One targeted web query through the configured search
chain; aggregator, social and ad hosts are never accepted, and the result must name the
opportunity."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.discovery.listing import host_of, official_search_query, pick_official_search_hit
from app.domain.discovery.models import registrable_host
from app.domain.ports.discovery import ISearchProvider

SEARCH_RESULTS = 8


@dataclass(slots=True)
class OfficialPortalSearch:
    search: ISearchProvider
    aggregators: frozenset[str] = frozenset()

    async def find(
        self, title: str, organization: str | None, *, not_url: str | None = None
    ) -> str | None:
        """The official application / programme URL, or None. `not_url`: the current (dead or
        aggregator) portal - a result on the same site is not an improvement."""
        try:
            hits = await self.search.search(
                official_search_query(title, organization), SEARCH_RESULTS
            )
        except Exception:  # noqa: BLE001 - search is best effort; the caller keeps the old URL
            return None
        exclude = (
            frozenset({host_of(not_url), registrable_host(not_url)}) if not_url else frozenset()
        )
        return pick_official_search_hit(
            [(h.url, h.title) for h in hits], title, organization, self.aggregators, exclude
        )
