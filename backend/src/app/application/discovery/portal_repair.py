"""Queue repair: point applications at the official portal instead of an aggregator page.

Opportunities discovered before aggregator resolution existed (or whose page offered no outbound
link at the time) may still carry a blog / directory URL as `portal_url`. This re-reads each
such page (SSRF-guarded crawler), takes the best outbound official / application link, and
updates the opportunity so its queued or blocked application goes to the real portal."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.discovery.portal_search import OfficialPortalSearch
from app.application.discovery.use_cases import DiscoveryDeps
from app.domain.discovery.listing import (
    is_aggregator_url,
    resolve_portal_url,
    title_tokens,
)
from app.domain.discovery.models import FetchedPage
from app.domain.events.models import NewEvent
from app.domain.shared.enums import ApplicationState

REPAIRABLE = [
    ApplicationState.QUEUED,
    ApplicationState.BLOCKED_NEEDS_HUMAN,
    ApplicationState.PAUSED_NEEDS_USER_INPUT,
]


@dataclass(frozen=True, slots=True)
class PortalRepair:
    opportunity_id: uuid.UUID
    title: str
    old_url: str
    new_url: str | None
    reason: str


@dataclass(slots=True)
class RepairAggregatorPortals:
    deps: DiscoveryDeps

    async def execute(self, user_id: uuid.UUID) -> list[PortalRepair]:
        extra = self.deps.aggregator_domains
        async with self.deps.uow_factory(user_id) as uow:
            pairs = await uow.app_details.list_by_states(user_id, REPAIRABLE)
        targets = [opp for _, opp in pairs if is_aggregator_url(opp.portal_url, extra)]
        results: list[PortalRepair] = []
        if not targets:
            return results
        crawler = self.deps.crawler_factory.create(None)
        try:
            for opp in targets:
                old = opp.portal_url or ""
                try:
                    fetched = await crawler.fetch(old, None)
                except Exception as exc:  # noqa: BLE001 - one page never stops the repair
                    fetched = FetchedPage(old, old, None, "", error=f"fetch: {exc}"[:200])
                if not fetched.html:
                    reason = fetched.error or f"HTTP {fetched.status}"
                    found = await self._search(opp.title, opp.organization, old)
                    if found is not None:
                        await self._save(user_id, opp.id, opp.title, old, found)
                        results.append(PortalRepair(opp.id, opp.title, old, found, "web search"))
                    else:
                        results.append(PortalRepair(opp.id, opp.title, old, None, reason[:200]))
                    continue
                page_url = fetched.final_url or old
                processed = self.deps.processor.process(fetched.html, page_url)
                # A page about this very opportunity (its title matches) may use its best link;
                # a listing must name the opportunity in the link itself.
                wanted = title_tokens(opp.title, opp.organization)
                on_page = title_tokens(processed.title or "")
                siblings = 1 if wanted and len(wanted & on_page) * 2 >= len(wanted) else 2
                new = resolve_portal_url(
                    old,
                    page_url,
                    processed.outbound_links,
                    title=opp.title,
                    organization=opp.organization,
                    siblings=siblings,
                    extra=extra,
                )
                via = "official link"
                if new is None:
                    new = await self._search(opp.title, opp.organization, old)
                    via = "web search"
                if new is None:
                    reason = (
                        "no official link on the page or in search"
                        if page_url == old
                        else f"no official link (the page now redirects to {page_url})"
                    )
                    results.append(PortalRepair(opp.id, opp.title, old, None, reason))
                    continue
                await self._save(user_id, opp.id, opp.title, old, new)
                results.append(PortalRepair(opp.id, opp.title, old, new, via))
        finally:
            await crawler.aclose()
        return results

    async def _search(self, title: str, organization: str | None, old: str) -> str | None:
        finder = OfficialPortalSearch(self.deps.search, self.deps.aggregator_domains)
        return await finder.find(title, organization, not_url=old)

    async def _save(
        self, user_id: uuid.UUID, opp_id: uuid.UUID, title: str, old: str, new: str
    ) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            await uow.opportunities.set_portal_url(user_id, opp_id, new)
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="opportunity.portal_resolved",
                    payload={"opportunity_id": str(opp_id), "title": title, "old": old, "new": new},
                )
            )
            await uow.commit()
