"""Deterministic fake chat provider standing in for the user's configured LLM.

It answers extraction prompts by reading "Field: value" lines from the <untrusted_document>
(the way a real model reads the page) and fuzzy-eligibility prompts with a simple rule, and it
records every prompt so tests can assert what the model was (and was not) shown."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from app.domain.llm.provider import ConnectionErrorCode, HandshakeResult
from app.domain.ports.llm import ChatMessage, Completion
from app.infrastructure.llm.http_client import LLMHttpError

FIELD = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z ]{2,20}):\s*(.+?)\s*$", re.M)


def _one_opportunity(block: str) -> dict[str, object] | None:
    fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(block)}
    if "title" not in fields:
        return None
    gpa, scale = None, None
    if "min gpa" in fields:
        g, _, s = fields["min gpa"].partition("/")
        gpa, scale = float(g), float(s or 4)
    nats = (
        []
        if fields.get("nationality", "").lower() in ("", "any")
        else [n.strip() for n in fields["nationality"].split(",")]
    )
    return {
        "title": fields["title"],
        "organization": fields.get("organization"),
        "type": fields.get("type", "OTHER"),
        "deadline": fields.get("deadline"),
        "deadline_raw": fields.get("deadline"),
        "portal_url": fields.get("apply"),
        "summary": "Fixture opportunity extracted by the fake model.",
        "eligibility": {
            "degree_levels": [fields["degree"]] if "degree" in fields else [],
            "min_gpa": gpa,
            "gpa_scale": scale,
            "nationalities": nats,
            "other_criteria": [fields["other"]] if "other" in fields else [],
            **_v2_rules(fields),
        },
    }


def _v2_rules(fields: dict[str, str]) -> dict[str, object]:
    """Profile v2 criteria lines: "English: IELTS 6.5", "MOI: accepted", "Visa sponsorship: no",
    "Passport: required", "Research areas: robotics, vision", "Work mode: remote"."""
    out: dict[str, object] = {}
    if "english" in fields:
        test, _, score = fields["english"].partition(" ")
        if score.replace(".", "", 1).isdigit():
            out["english_tests"] = {test.upper(): float(score)}
    if "moi" in fields:
        out["moi_accepted"] = not fields["moi"].lower().startswith("not")
    if "visa sponsorship" in fields:
        out["visa_sponsorship"] = fields["visa sponsorship"].lower() in ("yes", "provided")
    if "passport" in fields:
        out["requires_passport"] = fields["passport"].lower() == "required"
    if "research areas" in fields:
        out["research_areas"] = [a.strip() for a in fields["research areas"].split(",")]
    if "work mode" in fields:
        out["work_mode"] = fields["work mode"].upper()
    return out


TITLE_SPLIT = re.compile(r"(?=^\s*(?:[-*]\s*)?Title:)", re.M)


def _extraction_reply(document: str) -> str:
    """One opportunity per "Title:" block, like a model reading a multi-item page."""
    opps = [o for o in (_one_opportunity(b) for b in TITLE_SPLIT.split(document)) if o]
    return "```json\n" + json.dumps({"opportunities": opps}) + "\n```"


EVIDENCE_LINE = re.compile(r"^\[(\d+)\][^\n]*$", re.M)


def _evidence_reply(user: str) -> str:
    """Answer like a careful model: find an evidence line whose label shares the question's key
    words ("Programming languages: ...") and answer with its value; otherwise not answerable."""
    question = re.search(r"<question>(.*?)</question>", user, re.S)
    q_words = {
        w
        for w in re.findall(r"[a-z]+", (question.group(1) if question else "").lower())
        if len(w) > 3 and w not in {"which", "what", "most", "comfortable", "with", "your", "are"}
    }
    blocks = re.split(r"\n(?=\[\d+\] )", user)
    for block in blocks:
        num = re.match(r"\[(\d+)\]", block.strip())
        for line in block.splitlines():
            label, sep, value = line.partition(":")
            label_words = set(re.findall(r"[a-z]+", label.lower()))
            if sep and value.strip() and len(q_words & label_words) >= 2:
                return json.dumps(
                    {
                        "answerable": True,
                        "answer": value.strip(),
                        "confidence": 0.92,
                        "evidence_ids": [int(num.group(1))] if num else [],
                    }
                )
    return json.dumps({"answerable": False, "answer": "", "confidence": 0.0, "evidence_ids": []})


# Labels a pattern-matching filler cannot understand; the fake "model" knows what they mean.
NAV_ADVANCE = ("begin my journey", "onwards", "let's go", "carry on")


def _nav_reply(user: str) -> str:
    """Navigator decisions like a sensible model: report the final review, accept a cookie
    banner, click an element that moves the application forward, else give up."""
    if "review your answers" in user.lower():
        return json.dumps({"action": "reached_final_review", "index": None, "reason": "review"})
    for line in user.splitlines():
        m = re.match(r"\[(\d+)\] \([^)]*\) (.+)$", line.strip())
        if not m:
            continue
        label = m.group(2).lower()
        if "[banner/dialog]" in label and ("accept" in label or "agree" in label):
            return json.dumps({"action": "dismiss_cookie_banner", "index": int(m.group(1))})
        if any(word in label for word in NAV_ADVANCE):
            return json.dumps({"action": "click", "index": int(m.group(1)), "reason": "advance"})
    return json.dumps({"action": "give_up", "index": None, "reason": "nothing relevant"})


def _fuzzy_reply(user: str) -> str:
    criteria = re.findall(r"^\d+\.\s*(.+)$", user, re.M)
    has_experience = '"experiences": [' in user and '"experiences": []' not in user
    results = [
        {
            "criterion": c,
            "status": "PASS" if ("leadership" in c.lower() and has_experience) else "UNKNOWN",
            "evidence": "profile lists a leadership role" if has_experience else "not stated",
        }
        for c in criteria
    ]
    return json.dumps({"results": results})


ESSAY_TEXT = (
    "I grew up repairing radios with my father. It taught me patience. When our neighbourhood lost "
    "power for a week, I built a small solar charger from spare parts, and it worked badly at first "
    "but it worked. That stubborn loop of trying, failing, and trying again is why research appeals "
    "to me. In this program I want to study how small devices can make energy cheaper. I am not the "
    "loudest student in the room. I am usually the one still testing at midnight."
)

TRUNCATE_ABOVE_CHARS = 6_500  # windows larger than this simulate finish_reason=length
SLOW_PAGE_SECONDS = 60.0  # extraction of a page containing "SLOW-PAGE" hangs this long


@dataclass
class FakeLLM:
    model: str
    calls: list[list[ChatMessage]] = field(default_factory=list)
    finish_reasons: list[str | None] = field(default_factory=list)
    # (code, HTTP status): every call is refused like a revoked key / disabled model.
    reject: tuple[ConnectionErrorCode, int] | None = None

    async def complete(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 1024, temperature: float = 0.2
    ) -> Completion:
        self.calls.append(list(messages))
        if self.reject is not None:
            code, status = self.reject
            raise LLMHttpError(code, f"HTTP {status}: Forbidden", status)
        system = messages[0].content
        user = messages[-1].content
        finish: str | None = "stop"
        if "extract structured opportunity" in system:
            document = messages[1].content
            if "SLOW-PAGE" in document:
                await asyncio.sleep(SLOW_PAGE_SECONDS)  # a model that thinks for minutes
            if "SLOW-WINDOW" in document:
                raise LLMHttpError(ConnectionErrorCode.TIMEOUT, "request timed out")
            if len(document) > TRUNCATE_ABOVE_CHARS:
                # A reasoning model ran out of output tokens mid-answer.
                text, finish = '{"opportunities": [{"title": "Cut off mid', "length"
            else:
                text = _extraction_reply(user if len(messages) == 2 else document)
        elif "answer ONE question from a student's application form" in system:
            text = _evidence_reply(user)
        elif "check a student's eligibility" in system:
            text = _fuzzy_reply(user)
        elif "IN THE VOICE OF ONE SPECIFIC STUDENT" in system:
            # First draft slips in two banned cliches; the revision request removes them.
            text = (
                ESSAY_TEXT
                if user.startswith("Revise the essay")
                else (
                    "I want to delve into research; it is a testament to my curiosity. "
                    + ESSAY_TEXT
                )
            )
        elif "You operate a web browser" in system:
            text = _nav_reply(user)
        elif "List claims in <essay>" in system:
            text = '{"unsupported_claims": []}'
        else:
            text = "ok"
        self.finish_reasons.append(finish)
        return Completion(
            text=text,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=10,
            finish_reason=finish,
        )

    async def list_models(self) -> list[str]:
        return [self.model]

    async def handshake(self) -> HandshakeResult:
        return HandshakeResult(True, 1, self.model, True, None, None)


class FakeLLMFactory:
    def __init__(self) -> None:
        self.http_client = httpx.AsyncClient()
        self.created: list[FakeLLM] = []
        self.reject: tuple[ConnectionErrorCode, int] | None = None

    def create(
        self, *, base_url: str, api_key: str, model: str, rpm_limit: int | None = None
    ) -> FakeLLM:
        llm = FakeLLM(model=model, reject=self.reject)
        self.created.append(llm)
        return llm

    @property
    def call_count(self) -> int:
        return sum(len(llm.calls) for llm in self.created)

    @property
    def models_used(self) -> set[str]:
        return {llm.model for llm in self.created if llm.calls}

    @property
    def documents_seen(self) -> list[str]:
        return [m.content for llm in self.created for call in llm.calls for m in call]

    async def aclose(self) -> None:
        await self.http_client.aclose()
