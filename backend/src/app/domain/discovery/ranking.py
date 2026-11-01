"""Deterministic opportunity ranking: a 0-100 match score with an explainable breakdown.

  eligibility  (<= 40)  latest verdict: eligible > uncertain > not evaluated > ineligible/expired
  alignment    (<= 25)  research interests, major / minor / thesis / skills vs the listing;
                        scholarships, internships and research rank above generic job posts
  portal       (<= 20)  ready-to-fill official portal (ATS > official domain > other) vs an
                        aggregator / blog page or no portal at all
  urgency      (<= 15)  closer (still open) deadlines first

Pure function of the opportunity, the student profile and the clock: nothing is stored, so the
score always reflects the current profile."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.discovery.listing import (
    host_of,
    is_aggregator_url,
    is_ats_url,
    title_tokens,
)
from app.domain.discovery.models import Opportunity, registrable_host
from app.domain.profile.models import StudentProfile
from app.domain.shared.enums import EligibilityOutcome, OpportunityType

ELIGIBILITY_POINTS = {
    EligibilityOutcome.ELIGIBLE: 40,
    EligibilityOutcome.UNCERTAIN: 22,
    EligibilityOutcome.INELIGIBLE: 4,
    EligibilityOutcome.EXPIRED: 0,
}
NOT_EVALUATED_POINTS = 15
TYPE_POINTS = {
    OpportunityType.SCHOLARSHIP: 6,
    OpportunityType.INTERNSHIP: 6,
    OpportunityType.RESEARCH: 6,
    OpportunityType.REMOTE_JOB: 3,
    OpportunityType.OTHER: 0,
}
_OFFICIAL = re.compile(r"\.(edu|gov|mil|int)(\.[a-z]{2})?$|\.ac\.[a-z]{2}$|\.org$")
SORT_KEYS = ("match", "deadline", "title", "status")


@dataclass(frozen=True, slots=True)
class RankBreakdown:
    eligibility: int
    alignment: int
    portal: int
    urgency: int
    reasons: tuple[str, ...] = ()

    @property
    def score(self) -> int:
        return max(0, min(100, self.eligibility + self.alignment + self.portal + self.urgency))

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligibility": self.eligibility,
            "alignment": self.alignment,
            "portal": self.portal,
            "urgency": self.urgency,
            "reasons": list(self.reasons),
        }


def _profile_tokens(profile: StudentProfile | None) -> tuple[frozenset[str], frozenset[str]]:
    """(research interest tokens, other academic tokens) of the student."""
    if profile is None:
        return frozenset(), frozenset()
    d = profile.details
    research = title_tokens(" ".join([*d.research_interests, d.thesis_or_fyp_title or ""]))
    academic = title_tokens(
        " ".join(
            [
                profile.major or "",
                d.minor_or_specialization or "",
                *profile.skills[:30],
            ]
        )
    )
    return research, academic - research


def _portal_points(opp: Opportunity, extra: frozenset[str]) -> tuple[int, str]:
    url = opp.portal_url
    if not url:
        return 0, "no application portal"
    if is_aggregator_url(url, extra):
        return 4, "aggregator / blog page"
    if is_ats_url(url):
        return 20, "application system (ATS)"
    if _OFFICIAL.search(host_of(url)):
        return 17, "official domain"
    if opp.source_url and registrable_host(url) == registrable_host(opp.source_url):
        return 10, "the listing page itself"
    return 13, "programme website"


def _urgency_points(deadline: datetime | None, now: datetime) -> tuple[int, str]:
    if deadline is None:
        return 5, "no deadline stated"
    days = (deadline - now).days
    if days < 0:
        return 0, "deadline passed"
    for limit, points in ((7, 15), (30, 12), (60, 9), (120, 6)):
        if days <= limit:
            return points, f"{days} days left"
    return 3, f"{days} days left"


def rank_opportunity(
    opp: Opportunity,
    profile: StudentProfile | None,
    now: datetime,
    extra_aggregators: frozenset[str] = frozenset(),
) -> RankBreakdown:
    reasons: list[str] = []
    outcome = opp.latest_outcome
    eligibility = (
        ELIGIBILITY_POINTS.get(outcome, NOT_EVALUATED_POINTS) if outcome else NOT_EVALUATED_POINTS
    )
    reasons.append(f"verdict {outcome.value.lower() if outcome else 'pending'}")

    rules = opp.eligibility
    text = " ".join(
        [
            opp.title,
            opp.organization or "",
            opp.summary[:800],
            *rules.research_areas,
            *rules.other_criteria[:6],
        ]
    )
    listing = title_tokens(text)
    research, academic = _profile_tokens(profile)
    alignment = TYPE_POINTS.get(opp.type, 0)
    research_hits = sorted(research & listing)
    academic_hits = sorted(academic & listing)
    alignment += min(12, 6 * len(research_hits)) + min(8, 2 * len(academic_hits))
    if profile is not None and profile.degree_level in rules.degree_levels:
        alignment += 3
    mode = profile.details.preferred_work_mode if profile is not None else None
    if rules.work_mode and mode is not None and mode.value in (rules.work_mode, "ANY"):
        alignment += 2
    alignment = min(25, alignment)
    if research_hits or academic_hits:
        reasons.append("matches " + ", ".join((research_hits + academic_hits)[:5]))
    reasons.append(opp.type.value.lower())

    portal, portal_reason = _portal_points(opp, extra_aggregators)
    urgency, urgency_reason = _urgency_points(opp.deadline_at, now)
    if outcome is EligibilityOutcome.INELIGIBLE:
        urgency, urgency_reason = 0, "not eligible: deadline irrelevant"
    reasons += [portal_reason, urgency_reason]
    return RankBreakdown(eligibility, alignment, portal, urgency, tuple(reasons))


def sort_items[T](
    items: Sequence[T],
    sort: str | None,
    order: str,
    *,
    opportunity: Callable[[T], Opportunity],
    rank: Callable[[T], RankBreakdown],
    status: Callable[[T], str],
) -> list[T]:
    """Stable sort by match score / deadline / title / status; `order` is "asc" or "desc".
    Items without a deadline always go last."""
    if sort not in SORT_KEYS:
        return list(items)
    reverse = order == "desc"
    if sort == "deadline":
        dated = [i for i in items if opportunity(i).deadline_at is not None]
        undated = [i for i in items if opportunity(i).deadline_at is None]
        dated.sort(key=lambda i: opportunity(i).deadline_at or datetime.min, reverse=reverse)
        return dated + undated
    keys: dict[str, Callable[[T], Any]] = {
        "match": lambda i: rank(i).score,
        "title": lambda i: opportunity(i).title.lower(),
        "status": status,
    }
    return sorted(items, key=keys[sort], reverse=reverse)
