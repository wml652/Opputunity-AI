from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.applications.models import Application, Transition
from app.domain.discovery.models import (
    AiPolicy,
    Campaign,
    CampaignPhase,
    CampaignSettings,
    CampaignSource,
    CrawledPage,
    Opportunity,
    empty_counters,
)
from app.domain.discovery.playbook import DomainPlaybook, ExtractionStrategy, FetchStrategy
from app.domain.eligibility.rules import EligibilityRules, RuleResult, RuleSource, RuleStatus
from app.domain.ports.discovery import OpportunityFilter, Verdict
from app.domain.shared.enums import (
    ApplicationState,
    CampaignStatus,
    CrawlStatus,
    EligibilityOutcome,
)
from app.domain.shared.ids import new_uuid7
from app.infrastructure.db.models import (
    ApplicationRow,
    ApplicationTransitionRow,
    CampaignRow,
    CampaignSourceRow,
    CrawledPageRow,
    DomainPlaybookRow,
    EligibilityVerdictRow,
    OpportunityRow,
)
from app.infrastructure.db.repositories.vault import vector_literal

# ---- campaigns ----------------------------------------------------------------------------------


_DEDUPED_STATUSES = (CrawlStatus.DISTILLED, CrawlStatus.EXTRACTED)
_DEDUPED_CONTENT = text("content_sha256 IS NOT NULL AND status IN ('DISTILLED', 'EXTRACTED')")


class SqlCampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, campaign: Campaign) -> None:
        self._s.add(
            CampaignRow(
                id=campaign.id,
                user_id=campaign.user_id,
                name=campaign.name,
                scope=campaign.scope,
                keywords=campaign.keywords,
                status=campaign.status,
                phase=campaign.phase.value,
                settings=campaign.settings.to_dict(),
                counters=campaign.counters,
                error=campaign.error,
                created_at=campaign.created_at,
            )
        )
        await self._s.flush()
        for src in campaign.sources:
            self._s.add(
                CampaignSourceRow(
                    id=src.id,
                    user_id=src.user_id,
                    campaign_id=src.campaign_id,
                    source_type=src.source_type,
                    url=src.url,
                    enabled=src.enabled,
                )
            )
        await self._s.flush()

    async def _sources(
        self, user_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[CampaignSource]]:
        out: dict[uuid.UUID, list[CampaignSource]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = await self._s.scalars(
            select(CampaignSourceRow).where(
                CampaignSourceRow.user_id == user_id, CampaignSourceRow.campaign_id.in_(list(ids))
            )
        )
        for r in rows:
            out[r.campaign_id].append(
                CampaignSource(r.id, r.user_id, r.campaign_id, r.source_type, r.url, r.enabled)
            )
        return out

    @staticmethod
    def _campaign(row: CampaignRow, sources: list[CampaignSource]) -> Campaign:
        return Campaign(
            id=row.id,
            user_id=row.user_id,
            name=row.name,
            scope=row.scope,
            keywords=list(row.keywords),
            status=row.status,
            phase=CampaignPhase(row.phase),
            settings=CampaignSettings.from_dict(dict(row.settings)),
            counters={**empty_counters(), **dict(row.counters)},
            error=row.error,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            sources=sources,
        )

    async def get(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign | None:
        row = await self._s.scalar(
            select(CampaignRow).where(CampaignRow.user_id == user_id, CampaignRow.id == campaign_id)
        )
        if row is None:
            return None
        return self._campaign(row, (await self._sources(user_id, [row.id]))[row.id])

    async def list(self, user_id: uuid.UUID, limit: int) -> list[Campaign]:
        rows = list(
            await self._s.scalars(
                select(CampaignRow)
                .where(CampaignRow.user_id == user_id)
                .order_by(CampaignRow.created_at.desc())
                .limit(limit)
            )
        )
        sources = await self._sources(user_id, [r.id for r in rows])
        return [self._campaign(r, sources[r.id]) for r in rows]

    async def update_state(
        self,
        user_id: uuid.UUID,
        campaign_id: uuid.UUID,
        *,
        status: CampaignStatus | None = None,
        phase: CampaignPhase | None = None,
        counters: dict[str, int] | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if phase is not None:
            values["phase"] = phase.value
        if counters is not None:
            values["counters"] = counters
        if error is not None:
            values["error"] = error
        if started_at is not None:
            values["started_at"] = started_at
        if finished_at is not None:
            values["finished_at"] = finished_at
        if not values:
            return
        stmt = update(CampaignRow).where(
            CampaignRow.user_id == user_id, CampaignRow.id == campaign_id
        )
        if status is None and phase is None:
            # Counter updates never resurrect a cancelled campaign's state.
            stmt = stmt.where(CampaignRow.status != CampaignStatus.CANCELLED)
        await self._s.execute(stmt.values(**values))

    async def fail_running(self, error: str, at: datetime) -> int:
        result = await self._s.execute(
            update(CampaignRow)
            .where(CampaignRow.status.in_([CampaignStatus.RUNNING, CampaignStatus.DRAFT]))
            .values(
                status=CampaignStatus.FAILED,
                phase=CampaignPhase.FAILED.value,
                error=error,
                finished_at=at,
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def active_for_user(self, user_id: uuid.UUID) -> Campaign | None:
        row = await self._s.scalar(
            select(CampaignRow)
            .where(
                CampaignRow.user_id == user_id,
                CampaignRow.status.in_([CampaignStatus.RUNNING, CampaignStatus.DRAFT]),
            )
            .order_by(CampaignRow.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return self._campaign(row, (await self._sources(user_id, [row.id]))[row.id])

    async def status_of(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> CampaignStatus | None:
        return await self._s.scalar(
            select(CampaignRow.status).where(
                CampaignRow.user_id == user_id, CampaignRow.id == campaign_id
            )
        )


# ---- pages --------------------------------------------------------------------------------------


def _page(r: CrawledPageRow) -> CrawledPage:
    return CrawledPage(
        id=r.id,
        user_id=r.user_id,
        campaign_id=r.campaign_id,
        source_id=r.source_id,
        url=r.url,
        final_url=r.final_url,
        http_status=r.http_status,
        status=r.status,
        title=r.title,
        content_sha256=r.content_sha256,
        markdown=r.markdown,
        injection_flags=list(r.injection_flags or []),
        error=r.error,
        fetched_at=r.fetched_at,
        trace=list(r.trace or []),
        error_stage=r.error_stage,
        error_code=r.error_code,
        error_detail=r.error_detail,
        discovery_source=r.discovery_source,
        discovery_query=r.discovery_query,
        strategy=dict(r.strategy or {}),
    )


class SqlCrawledPageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, page: CrawledPage) -> bool:
        stmt = (
            pg_insert(CrawledPageRow)
            .values(
                id=page.id,
                user_id=page.user_id,
                campaign_id=page.campaign_id,
                source_id=page.source_id,
                url=page.url,
                final_url=page.final_url,
                http_status=page.http_status,
                status=page.status,
                title=page.title,
                content_sha256=page.content_sha256,
                markdown=page.markdown,
                injection_flags=page.injection_flags,
                error=page.error,
                fetched_at=page.fetched_at,
                trace=page.trace,
                error_stage=page.error_stage,
                error_code=page.error_code,
                error_detail=page.error_detail,
                discovery_source=page.discovery_source,
                discovery_query=page.discovery_query,
                strategy=page.strategy,
            )
            # Partial unique index: only successful content dedupes; failed/empty pages that
            # share an identical (empty) body are all kept.
            .on_conflict_do_nothing(
                index_elements=["campaign_id", "content_sha256"],
                index_where=_DEDUPED_CONTENT,
            )
            .returning(CrawledPageRow.id)
        )
        return (await self._s.scalar(stmt)) is not None

    async def for_campaign(
        self, user_id: uuid.UUID, campaign_id: uuid.UUID, statuses: Sequence[CrawlStatus] | None
    ) -> list[CrawledPage]:
        query = select(CrawledPageRow).where(
            CrawledPageRow.user_id == user_id, CrawledPageRow.campaign_id == campaign_id
        )
        if statuses:
            query = query.where(CrawledPageRow.status.in_(list(statuses)))
        rows = await self._s.scalars(query.order_by(CrawledPageRow.fetched_at))
        return [_page(r) for r in rows]

    async def get(self, user_id: uuid.UUID, page_id: uuid.UUID) -> CrawledPage | None:
        row = await self._s.scalar(
            select(CrawledPageRow).where(
                CrawledPageRow.user_id == user_id, CrawledPageRow.id == page_id
            )
        )
        return _page(row) if row else None

    async def update_diagnostics(self, page: CrawledPage) -> None:
        values: dict[str, Any] = {
            "status": page.status,
            "trace": page.trace,
            "error": page.error,
            "error_stage": page.error_stage,
            "error_code": page.error_code,
            "error_detail": page.error_detail,
            "strategy": page.strategy,
            "http_status": page.http_status,
            "final_url": page.final_url,
            "title": page.title,
            "markdown": page.markdown,
            "injection_flags": page.injection_flags,
        }
        # Keep the content hash unique per campaign: only take the new hash if it is free.
        clash = None
        if page.content_sha256 and page.status in _DEDUPED_STATUSES:
            clash = await self._s.scalar(
                select(CrawledPageRow.id).where(
                    CrawledPageRow.campaign_id == page.campaign_id,
                    CrawledPageRow.content_sha256 == page.content_sha256,
                    CrawledPageRow.status.in_(_DEDUPED_STATUSES),
                    CrawledPageRow.id != page.id,
                )
            )
        if clash is None:
            values["content_sha256"] = page.content_sha256
        await self._s.execute(
            update(CrawledPageRow)
            .where(CrawledPageRow.user_id == page.user_id, CrawledPageRow.id == page.id)
            .values(**values)
        )

    async def set_status(
        self, user_id: uuid.UUID, page_id: uuid.UUID, status: CrawlStatus, error: str | None = None
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if error is not None:
            values["error"] = error[:500]
        await self._s.execute(
            update(CrawledPageRow)
            .where(CrawledPageRow.user_id == user_id, CrawledPageRow.id == page_id)
            .values(**values)
        )


# ---- opportunities ------------------------------------------------------------------------------


def _opp(r: OpportunityRow) -> Opportunity:
    return Opportunity(
        id=r.id,
        user_id=r.user_id,
        campaign_id=r.campaign_id,
        page_id=r.page_id,
        type=r.type,
        title=r.title,
        organization=r.organization,
        location=r.location,
        is_international=r.is_international,
        deadline_at=r.deadline_at,
        deadline_raw=r.deadline_raw,
        portal_url=r.portal_url,
        source_url=r.source_url,
        summary=r.summary,
        eligibility=EligibilityRules.from_dict(dict(r.eligibility_rules)),
        ai_policy=AiPolicy(r.ai_policy),
        dedupe_key=r.dedupe_key,
        embedding_model=r.embedding_model,
        latest_outcome=r.latest_outcome,
        created_at=r.created_at,
    )


_INSERT_OPP = text(
    """
    INSERT INTO opportunities (id, user_id, campaign_id, page_id, type, title, organization,
        location, is_international, deadline_at, deadline_raw, portal_url, source_url, summary,
        eligibility_rules, ai_policy, dedupe_key, embedding, embedding_model, created_at,
        updated_at)
    VALUES (:id, :u, :c, :p, CAST(:type AS opportunity_type), :title, :org, :loc, :intl, :dl,
        :dlraw, :portal, :src, :summary, CAST(:rules AS jsonb), :ai, :key,
        CAST(CAST(:v AS text) AS vector), :model, :ts, :ts)
    ON CONFLICT ON CONSTRAINT uq_opportunities_user_dedupe DO NOTHING
    RETURNING id
    """
)

# Exact nearest neighbour within the tenant (see knowledge memory for why: pgvector 0.6.x).
_MOST_SIMILAR = text(
    """
    WITH mine AS MATERIALIZED (
        SELECT id, embedding FROM opportunities WHERE user_id = :u AND embedding_model = :m
    )
    SELECT id, 1 - (embedding <=> CAST(CAST(:v AS text) AS vector)) AS similarity
    FROM mine ORDER BY embedding <=> CAST(CAST(:v AS text) AS vector) LIMIT 1
    """
)


class SqlOpportunityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, opportunity: Opportunity, vector: Sequence[float]) -> bool:
        import json

        o = opportunity
        inserted = await self._s.scalar(
            _INSERT_OPP,
            {
                "id": o.id,
                "u": o.user_id,
                "c": o.campaign_id,
                "p": o.page_id,
                "type": o.type.value,
                "title": o.title,
                "org": o.organization,
                "loc": o.location,
                "intl": o.is_international,
                "dl": o.deadline_at,
                "dlraw": o.deadline_raw,
                "portal": o.portal_url,
                "src": o.source_url,
                "summary": o.summary,
                "rules": json.dumps(o.eligibility.to_dict()),
                "ai": o.ai_policy.value,
                "key": o.dedupe_key,
                "v": vector_literal(vector),
                "model": o.embedding_model,
                "ts": o.created_at,
            },
        )
        return inserted is not None

    async def recent_titles(
        self, user_id: uuid.UUID, limit: int
    ) -> list[tuple[uuid.UUID, str, str | None, str | None]]:
        rows = await self._s.execute(
            select(
                OpportunityRow.id,
                OpportunityRow.title,
                OpportunityRow.organization,
                OpportunityRow.portal_url,
            )
            .where(OpportunityRow.user_id == user_id)
            .order_by(OpportunityRow.created_at.desc())
            .limit(limit)
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    async def most_similar(
        self, user_id: uuid.UUID, vector: Sequence[float], embedding_model: str
    ) -> tuple[uuid.UUID, float] | None:
        row = (
            await self._s.execute(
                _MOST_SIMILAR, {"u": user_id, "m": embedding_model, "v": vector_literal(vector)}
            )
        ).first()
        return (row.id, float(row.similarity)) if row else None

    async def get(self, user_id: uuid.UUID, opportunity_id: uuid.UUID) -> Opportunity | None:
        row = await self._s.scalar(
            select(OpportunityRow).where(
                OpportunityRow.user_id == user_id, OpportunityRow.id == opportunity_id
            )
        )
        return _opp(row) if row else None

    async def search(self, user_id: uuid.UUID, flt: OpportunityFilter) -> list[Opportunity]:
        query = select(OpportunityRow).where(OpportunityRow.user_id == user_id)
        if flt.outcome is not None:
            query = query.where(OpportunityRow.latest_outcome == flt.outcome)
        if flt.campaign_id is not None:
            query = query.where(OpportunityRow.campaign_id == flt.campaign_id)
        if flt.search:
            pattern = f"%{flt.search.replace('%', r'\%').replace('_', r'\_')}%"
            query = query.where(
                or_(OpportunityRow.title.ilike(pattern), OpportunityRow.organization.ilike(pattern))
            )
        rows = await self._s.scalars(
            query.order_by(
                OpportunityRow.deadline_at.asc().nulls_last(), OpportunityRow.created_at.desc()
            ).limit(flt.limit)
        )
        return [_opp(r) for r in rows]

    async def for_campaign(self, user_id: uuid.UUID, campaign_id: uuid.UUID) -> list[Opportunity]:
        return await self.search(user_id, OpportunityFilter(campaign_id=campaign_id, limit=1000))

    async def set_outcome(
        self, user_id: uuid.UUID, opportunity_id: uuid.UUID, outcome: EligibilityOutcome
    ) -> None:
        await self._s.execute(
            update(OpportunityRow)
            .where(OpportunityRow.user_id == user_id, OpportunityRow.id == opportunity_id)
            .values(latest_outcome=outcome, updated_at=func.now())
        )

    async def counts(self, user_id: uuid.UUID) -> dict[str, int]:
        row = (
            await self._s.execute(
                text(
                    "SELECT (SELECT count(*) FROM crawled_pages WHERE user_id = :u "
                    "AND status IN ('DISTILLED', 'EXTRACTED')) AS pages_crawled, "
                    "(SELECT count(*) FROM opportunities WHERE user_id = :u) AS opportunities, "
                    "(SELECT count(*) FROM opportunities WHERE user_id = :u "
                    "AND latest_outcome = 'ELIGIBLE') AS eligible"
                ),
                {"u": user_id},
            )
        ).one()
        return {
            "pages_crawled": row.pages_crawled,
            "opportunities": row.opportunities,
            "eligible": row.eligible,
        }


# ---- verdicts -----------------------------------------------------------------------------------


class SqlVerdictRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, verdict: Verdict) -> None:
        self._s.add(
            EligibilityVerdictRow(
                id=verdict.id,
                user_id=verdict.user_id,
                opportunity_id=verdict.opportunity_id,
                outcome=verdict.outcome,
                reasons=[r.to_dict() for r in verdict.reasons],
                profile_snapshot_hash=verdict.profile_snapshot_hash,
                model=verdict.model,
                evaluated_at=verdict.evaluated_at,
            )
        )
        await self._s.flush()

    async def latest(self, user_id: uuid.UUID, opportunity_id: uuid.UUID) -> Verdict | None:
        row = await self._s.scalar(
            select(EligibilityVerdictRow)
            .where(
                EligibilityVerdictRow.user_id == user_id,
                EligibilityVerdictRow.opportunity_id == opportunity_id,
            )
            .order_by(EligibilityVerdictRow.evaluated_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return Verdict(
            id=row.id,
            user_id=row.user_id,
            opportunity_id=row.opportunity_id,
            outcome=row.outcome,
            reasons=[
                RuleResult(
                    r["rule"], RuleStatus(r["status"]), r["evidence"], RuleSource(r["source"])
                )
                for r in row.reasons
            ],
            profile_snapshot_hash=row.profile_snapshot_hash,
            model=row.model,
            evaluated_at=row.evaluated_at,
        )


# ---- applications -------------------------------------------------------------------------------


def _app(r: ApplicationRow) -> Application:
    return Application(
        id=r.id,
        user_id=r.user_id,
        opportunity_id=r.opportunity_id,
        state=r.state,
        priority_rank=r.priority_rank,
        deadline_at=r.deadline_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
        version=r.version,
    )


class SqlApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, application: Application) -> bool:
        a = application
        inserted = await self._s.scalar(
            pg_insert(ApplicationRow)
            .values(
                id=a.id,
                user_id=a.user_id,
                opportunity_id=a.opportunity_id,
                state=a.state,
                priority_rank=a.priority_rank,
                deadline_at=a.deadline_at,
                version=a.version,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            .on_conflict_do_nothing(constraint="uq_applications_user_opportunity")
            .returning(ApplicationRow.id)
        )
        return inserted is not None

    async def add_transition(self, user_id: uuid.UUID, transition: Transition) -> None:
        self._s.add(
            ApplicationTransitionRow(
                id=new_uuid7(),
                user_id=user_id,
                application_id=transition.application_id,
                from_state=transition.from_state,
                to_state=transition.to_state,
                actor=transition.actor,
                reason=transition.reason,
                at=transition.at,
            )
        )
        await self._s.flush()

    async def rerank_queue(self, user_id: uuid.UUID) -> None:
        await self._s.execute(
            text(
                """
                UPDATE applications a SET priority_rank = r.rank
                FROM (
                    SELECT id, row_number() OVER (
                        ORDER BY deadline_at ASC NULLS LAST, created_at ASC) AS rank
                    FROM applications WHERE user_id = :u AND state = 'QUEUED'
                ) r
                WHERE a.id = r.id AND a.user_id = :u
                """
            ),
            {"u": user_id},
        )

    async def queue(self, user_id: uuid.UUID, limit: int) -> list[tuple[Application, Opportunity]]:
        rows = (
            await self._s.execute(
                select(ApplicationRow, OpportunityRow)
                .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
                .where(ApplicationRow.user_id == user_id)
                .order_by(
                    ApplicationRow.priority_rank.asc().nulls_last(),
                    ApplicationRow.deadline_at.asc().nulls_last(),
                )
                .limit(limit)
            )
        ).all()
        return [(_app(a), _opp(o)) for a, o in rows]

    async def count_by_state(self, user_id: uuid.UUID) -> dict[ApplicationState, int]:
        rows = (
            await self._s.execute(
                select(ApplicationRow.state, func.count())
                .where(ApplicationRow.user_id == user_id)
                .group_by(ApplicationRow.state)
            )
        ).all()
        return {state: int(n) for state, n in rows}


# ---- domain playbooks (cross-campaign failure memory) -------------------------------------------


class SqlPlaybookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def all_for_user(self, user_id: uuid.UUID) -> dict[str, DomainPlaybook]:
        rows = await self._s.scalars(
            select(DomainPlaybookRow).where(DomainPlaybookRow.user_id == user_id)
        )
        return {
            r.domain: DomainPlaybook(
                id=r.id,
                user_id=r.user_id,
                domain=r.domain,
                fetch=FetchStrategy.from_dict(dict(r.fetch_strategy)),
                extraction=ExtractionStrategy.from_dict(dict(r.extraction_strategy)),
                failures=dict(r.failures),
                dead_urls=dict(r.dead_urls),
                successes=r.successes,
                last_campaign_id=r.last_campaign_id,
                updated_at=r.updated_at,
                bot_challenge=dict(r.bot_challenge) if r.bot_challenge else None,
            )
            for r in rows
        }

    async def save(self, playbook: DomainPlaybook) -> None:
        values = {
            "fetch_strategy": playbook.fetch.to_dict(),
            "extraction_strategy": playbook.extraction.to_dict(),
            "failures": playbook.failures,
            "dead_urls": playbook.dead_urls,
            "successes": playbook.successes,
            "last_campaign_id": playbook.last_campaign_id,
            "updated_at": playbook.updated_at,
            "bot_challenge": playbook.bot_challenge,
        }
        stmt = pg_insert(DomainPlaybookRow).values(
            id=playbook.id, user_id=playbook.user_id, domain=playbook.domain, **values
        )
        await self._s.execute(
            stmt.on_conflict_do_update(constraint="uq_domain_playbooks_user_domain", set_=values)
        )
