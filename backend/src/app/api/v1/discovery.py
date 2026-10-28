"""Discovery routes: campaigns, opportunities, eligibility evidence, application queue, pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import Container, CurrentUserId
from app.application.discovery.use_cases import CampaignInput, PipelineSummary
from app.domain.applications.models import Application
from app.domain.discovery.models import Campaign, CampaignSettings, CrawledPage, Opportunity
from app.domain.ports.discovery import OpportunityFilter, Verdict
from app.domain.shared.enums import (
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
    OpportunityType,
    Scope,
    SourceType,
)

router = APIRouter(tags=["discovery"])

# ---- schemas ------------------------------------------------------------------------------------


class CampaignIn(BaseModel):
    name: str = Field(default="", max_length=120)
    scope: Scope = Scope.BOTH
    keywords: list[str] = Field(min_length=1, max_length=20)
    target_urls: list[str] = Field(default_factory=list, max_length=20)
    use_websites: bool = True
    use_search: bool = True
    use_linkedin: bool = False
    max_pages_per_site: int = Field(default=15, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=4)
    search_results_per_query: int = Field(default=5, ge=1, le=20)
    max_search_pages: int = Field(default=20, ge=1, le=100)
    home_country: str | None = Field(default=None, max_length=100)


class SourceOut(BaseModel):
    id: uuid.UUID
    source_type: SourceType
    url: str | None


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    scope: Scope
    keywords: list[str]
    status: CampaignStatus
    phase: str
    counters: dict[str, int]
    settings: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    sources: list[SourceOut]

    @staticmethod
    def of(c: Campaign) -> CampaignOut:
        return CampaignOut(
            id=c.id,
            name=c.name,
            scope=c.scope,
            keywords=c.keywords,
            status=c.status,
            phase=c.phase.value,
            counters=c.counters,
            settings=c.settings.to_dict(),
            error=c.error,
            created_at=c.created_at,
            started_at=c.started_at,
            finished_at=c.finished_at,
            sources=[SourceOut(id=s.id, source_type=s.source_type, url=s.url) for s in c.sources],
        )


class PageOut(BaseModel):
    id: uuid.UUID
    url: str
    final_url: str | None
    http_status: int | None
    status: CrawlStatus
    title: str | None
    injection_flags: list[dict[str, str]]
    error: str | None
    markdown_chars: int
    fetched_at: datetime

    @staticmethod
    def of(p: CrawledPage) -> PageOut:
        return PageOut(
            id=p.id,
            url=p.url,
            final_url=p.final_url,
            http_status=p.http_status,
            status=p.status,
            title=p.title,
            injection_flags=p.injection_flags,
            error=p.error,
            markdown_chars=len(p.markdown or ""),
            fetched_at=p.fetched_at,
        )


class CampaignDetailOut(BaseModel):
    campaign: CampaignOut
    pages: list[PageOut]


class OpportunityOut(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID | None
    type: OpportunityType
    title: str
    organization: str | None
    location: str | None
    is_international: bool | None
    deadline_at: datetime | None
    deadline_raw: str | None
    portal_url: str | None
    source_url: str | None
    summary: str
    eligibility_rules: dict[str, Any]
    ai_policy: str
    latest_outcome: EligibilityOutcome | None
    created_at: datetime

    @staticmethod
    def of(o: Opportunity) -> OpportunityOut:
        return OpportunityOut(
            id=o.id,
            campaign_id=o.campaign_id,
            type=o.type,
            title=o.title,
            organization=o.organization,
            location=o.location,
            is_international=o.is_international,
            deadline_at=o.deadline_at,
            deadline_raw=o.deadline_raw,
            portal_url=o.portal_url,
            source_url=o.source_url,
            summary=o.summary,
            eligibility_rules=o.eligibility.to_dict(),
            ai_policy=o.ai_policy.value,
            latest_outcome=o.latest_outcome,
            created_at=o.created_at,
        )


class ReasonOut(BaseModel):
    rule: str
    status: str
    evidence: str
    source: str


class VerdictOut(BaseModel):
    outcome: EligibilityOutcome
    reasons: list[ReasonOut]
    model: str | None
    evaluated_at: datetime

    @staticmethod
    def of(v: Verdict) -> VerdictOut:
        return VerdictOut(
            outcome=v.outcome,
            reasons=[ReasonOut(**r.to_dict()) for r in v.reasons],
            model=v.model,
            evaluated_at=v.evaluated_at,
        )


class OpportunityDetailOut(BaseModel):
    opportunity: OpportunityOut
    verdict: VerdictOut | None


class QueueItemOut(BaseModel):
    application_id: uuid.UUID
    state: ApplicationState
    priority_rank: int | None
    deadline_at: datetime | None
    opportunity: OpportunityOut

    @staticmethod
    def of(app: Application, opp: Opportunity) -> QueueItemOut:
        return QueueItemOut(
            application_id=app.id,
            state=app.state,
            priority_rank=app.priority_rank,
            deadline_at=app.deadline_at,
            opportunity=OpportunityOut.of(opp),
        )


class PipelineSummaryOut(BaseModel):
    websites_crawled: int
    opportunities_found: int
    eligible: int
    needs_input: int
    ready_for_ok: int
    submitted: int
    queued: int
    latest_campaign: CampaignOut | None

    @staticmethod
    def of(s: PipelineSummary) -> PipelineSummaryOut:
        return PipelineSummaryOut(
            websites_crawled=s.websites_crawled,
            opportunities_found=s.opportunities_found,
            eligible=s.eligible,
            needs_input=s.needs_input,
            ready_for_ok=s.ready_for_ok,
            submitted=s.submitted,
            queued=s.queued,
            latest_campaign=CampaignOut.of(s.latest_campaign) if s.latest_campaign else None,
        )


# ---- routes -------------------------------------------------------------------------------------


@router.post("/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn, user_id: CurrentUserId, container: Container
) -> CampaignOut:
    campaign = await container.discovery.create_campaign.execute(
        user_id,
        CampaignInput(
            name=body.name,
            scope=body.scope,
            keywords=body.keywords,
            target_urls=body.target_urls,
            use_websites=body.use_websites,
            use_search=body.use_search,
            use_linkedin=body.use_linkedin,
            settings=CampaignSettings(
                max_pages_per_site=body.max_pages_per_site,
                max_depth=body.max_depth,
                search_results_per_query=body.search_results_per_query,
                max_search_pages=body.max_search_pages,
                home_country=body.home_country,
            ),
        ),
    )
    await container.discovery.runner.start(user_id, campaign.id)
    return CampaignOut.of(campaign)


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(user_id: CurrentUserId, container: Container) -> list[CampaignOut]:
    return [CampaignOut.of(c) for c in await container.discovery.list_campaigns.execute(user_id)]


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
async def get_campaign(
    campaign_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> CampaignDetailOut:
    detail = await container.discovery.get_campaign.execute(user_id, campaign_id)
    return CampaignDetailOut(
        campaign=CampaignOut.of(detail.campaign), pages=[PageOut.of(p) for p in detail.pages]
    )


@router.post("/campaigns/{campaign_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_campaign(
    campaign_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    await container.discovery.cancel_campaign.execute(user_id, campaign_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/opportunities", response_model=list[OpportunityOut])
async def list_opportunities(
    user_id: CurrentUserId,
    container: Container,
    outcome: EligibilityOutcome | None = None,
    campaign_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[OpportunityOut]:
    items = await container.discovery.list_opportunities.execute(
        user_id, OpportunityFilter(outcome=outcome, campaign_id=campaign_id, search=q, limit=limit)
    )
    return [OpportunityOut.of(o) for o in items]


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityDetailOut)
async def get_opportunity(
    opportunity_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> OpportunityDetailOut:
    opp, verdict = await container.discovery.get_opportunity.execute(user_id, opportunity_id)
    return OpportunityDetailOut(
        opportunity=OpportunityOut.of(opp), verdict=VerdictOut.of(verdict) if verdict else None
    )


@router.get("/applications/queue", response_model=list[QueueItemOut])
async def application_queue(user_id: CurrentUserId, container: Container) -> list[QueueItemOut]:
    return [QueueItemOut.of(a, o) for a, o in await container.discovery.queue.execute(user_id)]


@router.get("/pipeline/summary", response_model=PipelineSummaryOut)
async def pipeline_summary(user_id: CurrentUserId, container: Container) -> PipelineSummaryOut:
    return PipelineSummaryOut.of(await container.discovery.summary.execute(user_id))
