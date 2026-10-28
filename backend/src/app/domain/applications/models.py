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
    S.PREPARING: frozenset({S.FILLING}),
    S.FILLING: frozenset(
        {S.PAUSED_NEEDS_USER_INPUT, S.BLOCKED_NEEDS_HUMAN, S.AWAITING_FINAL_VERIFICATION}
    ),
    S.PAUSED_NEEDS_USER_INPUT: frozenset({S.RESUME_QUEUED}),
    S.BLOCKED_NEEDS_HUMAN: frozenset({S.RESUME_QUEUED}),
    S.RESUME_QUEUED: frozenset({S.FILLING}),
    S.AWAITING_FINAL_VERIFICATION: frozenset({S.SUBMIT_APPROVED, S.RESUME_QUEUED}),
    S.SUBMIT_APPROVED: frozenset({S.SUBMITTING}),
    S.SUBMITTING: frozenset({S.SUBMITTED, S.SUBMISSION_FAILED}),
    S.SUBMISSION_FAILED: frozenset({S.RESUME_QUEUED}),
}
# Any non-terminal state may be expired by the deadline sweep, cancelled by the user, or failed.
_ALWAYS = frozenset({S.EXPIRED, S.CANCELLED, S.FAILED})


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
    assert_transition(app.state, target)
    transition = Transition(app.id, app.state, target, actor, reason, at)
    app.state = target
    app.updated_at = at
    app.version += 1
    return transition
