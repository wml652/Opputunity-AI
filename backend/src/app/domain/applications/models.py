"""Application aggregate and its explicit state machine (illegal transitions raise)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.shared.enums import Actor, ApplicationState
from app.domain.shared.errors import ConflictError

S = ApplicationState

TERMINAL = frozenset({S.SUBMITTED, S.INELIGIBLE, S.EXPIRED, S.CANCELLED, S.FAILED})

TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    S.DISCOVERED: frozenset({S.ELIGIBILITY_PENDING}),
    S.ELIGIBILITY_PENDING: frozenset({S.ELIGIBLE, S.INELIGIBLE, S.EXPIRED}),
    S.ELIGIBLE: frozenset({S.QUEUED}),
    S.QUEUED: frozenset({S.PREPARING}),
    # BLOCKED_NEEDS_HUMAN from PREPARING: the lease reaper found a worker that died mid-run.
    S.PREPARING: frozenset({S.FILLING, S.BLOCKED_NEEDS_HUMAN}),
    S.FILLING: frozenset(
        {S.PAUSED_NEEDS_USER_INPUT, S.BLOCKED_NEEDS_HUMAN, S.AWAITING_FINAL_VERIFICATION}
    ),
    S.PAUSED_NEEDS_USER_INPUT: frozenset({S.RESUME_QUEUED}),
    S.BLOCKED_NEEDS_HUMAN: frozenset({S.RESUME_QUEUED}),
    S.RESUME_QUEUED: frozenset({S.FILLING}),
    S.AWAITING_FINAL_VERIFICATION: frozenset({S.SUBMIT_APPROVED, S.RESUME_QUEUED}),
    S.SUBMIT_APPROVED: frozenset({S.SUBMITTING}),
    # Back to AWAITING_FINAL_VERIFICATION when the page values differ from the approved ones:
    # the application is re-staged for review instead of being submitted.
    S.SUBMITTING: frozenset({S.SUBMITTED, S.SUBMISSION_FAILED, S.AWAITING_FINAL_VERIFICATION}),
    S.SUBMISSION_FAILED: frozenset({S.RESUME_QUEUED}),
}
# Any non-terminal state may be expired by the deadline sweep, cancelled by the user, or failed.
_ALWAYS = frozenset({S.EXPIRED, S.CANCELLED, S.FAILED})
# The student submitted the form THEMSELVES in Teach Mode (a human action, not the agent's):
# only Actor.USER may record it, from states where no agent submission is in flight.
SELF_SUBMIT_FROM = frozenset(
    {
        S.QUEUED,
        S.PAUSED_NEEDS_USER_INPUT,
        S.BLOCKED_NEEDS_HUMAN,
        S.RESUME_QUEUED,
        S.AWAITING_FINAL_VERIFICATION,
    }
)


class IllegalTransitionError(ConflictError):
    code = "illegal_transition"


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    if current in TERMINAL:
        return False
    return target in TRANSITIONS.get(current, frozenset()) or target in _ALWAYS


def assert_transition(current: ApplicationState, target: ApplicationState) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(f"cannot move application from {current} to {target}")


@dataclass(slots=True)
class Application:
    id: uuid.UUID
    user_id: uuid.UUID
    opportunity_id: uuid.UUID
    state: ApplicationState
    priority_rank: int | None
    deadline_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    last_error: str | None = None  # why the last run stopped (blocked / failed)
    resume_count: int = 0  # automatic resumes after a worker restart (capped)


@dataclass(frozen=True, slots=True)
class Transition:
    application_id: uuid.UUID
    from_state: ApplicationState | None
    to_state: ApplicationState
    actor: Actor
    reason: str
    at: datetime


def advance(
    app: Application, target: ApplicationState, *, actor: Actor, reason: str, at: datetime
) -> Transition:
    if not (target is S.SUBMITTED and actor is Actor.USER and app.state in SELF_SUBMIT_FROM):
        assert_transition(app.state, target)
    transition = Transition(app.id, app.state, target, actor, reason, at)
    app.state = target
    app.updated_at = at
    app.version += 1
    return transition
