"""Agent trajectories (the agent's experience) and the training-set builders.

A trajectory is one decision on one page: the page's DOM accessibility signature (headings,
button labels, input labels, URL path - PII-tokenized) and the action taken, with an optional
rejected action - a bot decision a human corrected in Teach Mode, i.e. a DPO preference pair.
They serve twice: retrieved by DOM similarity as demonstrations for new portals (DOM-RAG), and
exported as SFT / DPO datasets for fine-tuning a portal-navigation model."""

from __future__ import annotations

import enum
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

MAX_LIST = 25


class TrajectorySource(enum.StrEnum):
    TEACH = "teach"  # the student's own action in Teach Mode (ground truth)
    AUTO_SUCCESS = "auto_success"  # an autonomous decision on a run that reached Ready for OK
    AUTO_FAILURE = "auto_failure"  # an autonomous decision on a run that got blocked
    BACKFILL = "backfill"  # reconstructed from past runs / learned workflows


@dataclass(frozen=True, slots=True)
class DomState:
    """What a page offers, as a human would describe it."""

    path: str = ""
    title: str = ""
    headings: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()

    @staticmethod
    def of(
        url: str,
        title: str = "",
        headings: Sequence[str] = (),
        buttons: Sequence[str] = (),
        inputs: Sequence[str] = (),
    ) -> DomState:
        def clean(items: Sequence[str]) -> tuple[str, ...]:
            seen: list[str] = []
            for item in items:
                text = " ".join(str(item).split())[:80]
                if text and text not in seen:
                    seen.append(text)
            return tuple(seen[:MAX_LIST])

        try:
            path = urlsplit(url).path or "/"
        except ValueError:
            path = "/"
        return DomState(
            path[:200],
            " ".join(title.split())[:120],
            clean(headings),
            clean(buttons),
            clean(inputs),
        )

    def signature(self) -> str:
        """Normalized text used for embedding, retrieval and training prompts."""
        return (
            f"path: {self.path}\ntitle: {self.title}\n"
            f"headings: {' | '.join(self.headings)}\n"
            f"buttons: {' | '.join(self.buttons)}\n"
            f"inputs: {' | '.join(self.inputs)}"
        )


@dataclass(slots=True)
class Trajectory:
    id: uuid.UUID
    user_id: uuid.UUID
    source: TrajectorySource
    domain: str
    dom_signature: str  # PII-tokenized
    chosen_action: dict[str, Any]
    rejected_action: dict[str, Any] | None = None
    application_id: uuid.UUID | None = None
    embedding_model: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Demonstration:
    """A retrieved trajectory: how the student handled the most similar page before."""

    domain: str
    dom_signature: str
    action: dict[str, Any]
    similarity: float


@dataclass(frozen=True, slots=True)
class TrainingStats:
    total: int
    by_source: dict[str, int] = field(default_factory=dict)
    dpo_pairs: int = 0
    rag_decisions: int = 0
    llm_decisions: int = 0

    @property
    def rag_hit_rate(self) -> float:
        decided = self.rag_decisions + self.llm_decisions
        return round(100.0 * self.rag_decisions / decided, 1) if decided else 0.0


# ---- actions ---------------------------------------------------------------------------------


def click_action(label: str, role: str = "button", selector: str = "") -> dict[str, Any]:
    return {"action": "click", "role": role or "button", "label": label[:120], "selector": selector}


def fill_action(
    label: str, value_token: str, field_type: str = "text", selector: str = ""
) -> dict[str, Any]:
    kind = "select" if field_type in ("select", "radio") else "fill"
    return {
        "action": kind,
        "role": "combobox" if kind == "select" else "textbox",
        "label": label[:200],
        "selector": selector,
        "value_token": value_token,
    }


def action_json(action: dict[str, Any]) -> str:
    return json.dumps(action, sort_keys=True, ensure_ascii=False)


# ---- datasets ----------------------------------------------------------------------------------

TRAINING_SYSTEM = (
    "You are a browser agent that completes university, scholarship, internship and job "
    "application portals for a student. Given the current page (DOM accessibility signature) "
    "reply with exactly one JSON action: click a control by its visible label, fill / select a "
    "field with a symbolic profile token such as {{PROFILE.FULL_NAME}}, or stop before the final "
    "submit. Never click a final Submit button, never type passwords, never fill newsletter, "
    "lead-capture or search forms."
)

_DECOY = re.compile(
    r"subscribe|newsletter|share|facebook|twitter|whatsapp|login|log in|sign in|counsel|"
    r"inquiry|enquiry|search|cookie|privacy|contact",
    re.I,
)
_SWAP_TOKENS = (
    "{{PROFILE.EMAIL}}",
    "{{PROFILE.PHONE}}",
    "{{PROFILE.FULL_NAME}}",
    "{{PROFILE.GPA}}",
)


def _buttons(signature: str) -> list[str]:
    for line in signature.splitlines():
        if line.startswith("buttons:"):
            return [b.strip() for b in line[len("buttons:") :].split("|") if b.strip()]
    return []


def sft_record(t: Trajectory) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": TRAINING_SYSTEM},
            {"role": "user", "content": f"<page>\n{t.dom_signature}\n</page>"},
            {"role": "assistant", "content": action_json(t.chosen_action)},
        ],
        "metadata": {"source": t.source.value, "domain": t.domain},
    }


def synthetic_negative(t: Trajectory) -> dict[str, Any] | None:
    """A plausible wrong action for the same page: a decoy control (newsletter, share, login...)
    or another button for clicks; a swapped profile token for fills."""
    chosen = t.chosen_action
    if chosen.get("action") == "click":
        others = [b for b in _buttons(t.dom_signature) if b != chosen.get("label")]
        decoys = [b for b in others if _DECOY.search(b)] or others
        if not decoys:
            return None
        return click_action(decoys[0], "button")
    token = str(chosen.get("value_token") or "")
    if token.startswith("{{PROFILE."):
        swap = next((s for s in _SWAP_TOKENS if s != token), None)
        if swap:
            return {**chosen, "value_token": swap}
    return None


def dpo_records(trajectories: Sequence[Trajectory]) -> list[dict[str, Any]]:
    """Human corrections first (real preference pairs), then synthetic hard negatives for
    Teach Mode ground truth."""
    out: list[dict[str, Any]] = []
    for t in trajectories:
        rejected = t.rejected_action
        synthetic = False
        if rejected is None and t.source is TrajectorySource.TEACH:
            rejected = synthetic_negative(t)
            synthetic = True
        if rejected is None or action_json(rejected) == action_json(t.chosen_action):
            continue
        out.append(
            {
                "prompt": f"{TRAINING_SYSTEM}\n\n<page>\n{t.dom_signature}\n</page>",
                "chosen": action_json(t.chosen_action),
                "rejected": action_json(rejected),
                "metadata": {"source": t.source.value, "domain": t.domain, "synthetic": synthetic},
            }
        )
    return out
