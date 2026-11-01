"""The agent brain: portal workflows learned by demonstration (Teach Mode) and the LLM navigator's
decision type. Pure domain (stdlib only).

A *workflow* is what the student showed the agent on one portal: the clicks that lead from the
portal URL to the application form (resilient multi-strategy locators) and how each field was
answered. Personal values are never stored here - a field the profile mapper recognises as PII
keeps only its profile path; custom answers live in semantic memory (knowledge_memory_qa) and
are linked by id; passwords are never recorded at all."""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.domain.training.models import DomState


class AtsType(enum.StrEnum):
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    SLATE = "slate"
    GOOGLE_FORMS = "google_forms"
    GENERIC = "generic_portal"


_ATS_HOSTS: tuple[tuple[AtsType, re.Pattern[str]], ...] = (
    (AtsType.WORKDAY, re.compile(r"myworkdayjobs\.com|workday\.com", re.I)),
    (AtsType.GREENHOUSE, re.compile(r"greenhouse\.io", re.I)),
    (AtsType.LEVER, re.compile(r"lever\.co", re.I)),
    (AtsType.SLATE, re.compile(r"technolutions\.net|slate\.org|/apply/(form|frm)|/portal/", re.I)),
    (AtsType.GOOGLE_FORMS, re.compile(r"docs\.google\.com/forms|forms\.gle", re.I)),
)


def detect_ats(url: str) -> AtsType:
    for ats, pattern in _ATS_HOSTS:
        if pattern.search(url):
            return ats
    return AtsType.GENERIC


def workflow_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class StepKind(enum.StrEnum):
    CLICK = "click"
    NAVIGATE = "navigate"
    OPEN_TAB = "open_tab"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    UPLOAD = "upload"
    SUBMIT = "submit"


NAVIGATION_KINDS = frozenset({StepKind.CLICK, StepKind.NAVIGATE, StepKind.OPEN_TAB})
FIELD_KINDS = frozenset({StepKind.FILL, StepKind.SELECT, StepKind.CHECK, StepKind.UPLOAD})


@dataclass(frozen=True, slots=True)
class Locator:
    """Several ways to find the same element again (tried in this order when replaying)."""

    role: str = ""
    name: str = ""  # accessible name / visible text
    label: str = ""
    placeholder: str = ""
    css: str = ""
    href: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> Locator:
        d = d or {}
        return Locator(
            **{
                k: str(d.get(k) or "")[:300]
                for k in ("role", "name", "label", "placeholder", "css", "href")
            }
        )


@dataclass(frozen=True, slots=True)
class TeachStep:
    kind: StepKind
    url: str
    locator: Locator = field(default_factory=Locator)
    field_label: str = ""
    field_name: str = ""
    field_type: str = ""
    value: str | None = None  # None for passwords; personal values are dropped before storage
    secret: bool = False
    dom_signature: str = ""  # the page when the step happened (raw: tokenize before storing)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> TeachStep | None:
        try:
            kind = StepKind(str(d.get("kind") or d.get("type")))
        except ValueError:
            return None
        secret = bool(d.get("secret")) or str(d.get("field_type") or "") == "password"
        value = d.get("value")
        dom = d.get("dom")
        signature = str(d.get("dom_signature") or "")
        if isinstance(dom, dict):
            signature = DomState.of(
                str(d.get("url") or ""),
                str(dom.get("title") or ""),
                [str(x) for x in dom.get("headings") or []],
                [str(x) for x in dom.get("buttons") or []],
                [str(x) for x in dom.get("inputs") or []],
            ).signature()
        return TeachStep(
            kind=kind,
            url=str(d.get("url") or "")[:2000],
            locator=Locator.from_dict(d.get("locator")),
            field_label=str(d.get("field_label") or "")[:300],
            field_name=str(d.get("field_name") or "")[:200],
            field_type=str(d.get("field_type") or "")[:40],
            value=None if secret or value is None else str(value)[:4000],
            secret=secret,
            dom_signature=signature[:4000],
        )


@dataclass(frozen=True, slots=True)
class FieldMapping:
    field_label: str
    field_name: str
    field_type: str
    user_value: str | None = None  # only non-personal answers
    mapped_profile_path: str | None = None
    memory_qa_id: str | None = None

    def key(self) -> str:
        return (self.field_name or self.field_label).strip().lower()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> FieldMapping:
        return FieldMapping(
            field_label=str(d.get("field_label") or ""),
            field_name=str(d.get("field_name") or ""),
            field_type=str(d.get("field_type") or ""),
            user_value=d.get("user_value"),
            mapped_profile_path=d.get("mapped_profile_path"),
            memory_qa_id=d.get("memory_qa_id"),
        )


@dataclass(slots=True)
class AgentWorkflow:
    id: uuid.UUID
    user_id: uuid.UUID
    domain: str
    ats_type: AtsType
    navigation_steps: list[TeachStep]
    field_mappings: list[FieldMapping]
    source_application_id: uuid.UUID | None
    times_used: int = 0
    success_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def merge(self, navigation: list[TeachStep], mappings: list[FieldMapping]) -> None:
        """A newer demonstration wins: a non-empty path replaces the old one; field mappings
        are merged per field."""
        if navigation:
            self.navigation_steps = navigation
        merged = {m.key(): m for m in self.field_mappings}
        merged.update({m.key(): m for m in mappings if m.key()})
        self.field_mappings = list(merged.values())


MAX_PATH_STEPS = 12


def navigation_path(steps: list[TeachStep]) -> list[TeachStep]:
    """The demonstrated route from the portal URL to the form: navigation steps before the first
    field the student filled (consecutive duplicates removed)."""
    path: list[TeachStep] = []
    for step in steps:
        if step.kind in FIELD_KINDS:
            break
        if step.kind not in NAVIGATION_KINDS:
            continue
        if (
            path
            and path[-1].kind == step.kind
            and path[-1].locator == step.locator
            and (path[-1].url == step.url)
        ):
            continue
        path.append(step)
    # A navigate that merely follows the click before it adds nothing to a replay.
    pruned = [
        s
        for i, s in enumerate(path)
        if not (s.kind is StepKind.NAVIGATE and i > 0 and path[i - 1].kind is StepKind.CLICK)
    ]
    return pruned[:MAX_PATH_STEPS]


def final_field_values(steps: list[TeachStep]) -> list[TeachStep]:
    """The last value the student left in each field (typing produces many events)."""
    last: dict[str, TeachStep] = {}
    for step in steps:
        if step.kind in FIELD_KINDS and (step.field_name or step.field_label):
            last[(step.field_name or step.field_label).strip().lower()] = step
    return list(last.values())


_CONFIRMED = re.compile(
    r"thank you for (your )?(application|applying|submitting)|application (has been |was )?"
    r"(submitted|received)|we (have )?received your application|successfully submitted|"
    r"submission (is )?complete|confirmation (number|code|id)",
    re.I,
)


def looks_submitted(final_text: str, steps: list[TeachStep]) -> bool:
    """The student submitted the form themselves: a submit / click happened and the final page
    reads like a confirmation."""
    acted = any(s.kind in (StepKind.SUBMIT, StepKind.CLICK) for s in steps)
    return acted and bool(_CONFIRMED.search(final_text or ""))


# ---- LLM navigator ----------------------------------------------------------------------------


class NavAction(enum.StrEnum):
    CLICK = "click"
    DISMISS_COOKIE_BANNER = "dismiss_cookie_banner"
    REACHED_FINAL_REVIEW = "reached_final_review"
    NEEDS_HUMAN_LOGIN = "needs_human_login"
    GIVE_UP = "give_up"


@dataclass(frozen=True, slots=True)
class NavDecision:
    action: NavAction
    index: int | None = None
    reason: str = ""
