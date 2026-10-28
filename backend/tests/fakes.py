"""Deterministic fake chat provider standing in for the user's configured LLM.

It answers extraction prompts by reading "Field: value" lines from the <untrusted_document>
(the way a real model reads the page) and fuzzy-eligibility prompts with a simple rule, and it
records every prompt so tests can assert what the model was (and was not) shown."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from app.domain.llm.provider import HandshakeResult
from app.domain.ports.llm import ChatMessage, Completion

FIELD = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z ]{2,20}):\s*(.+?)\s*$", re.M)


def _extraction_reply(document: str) -> str:
    fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(document)}
    if "title" not in fields:
        return json.dumps({"opportunities": []})
    gpa, scale = None, None
    if "min gpa" in fields:
        g, _, s = fields["min gpa"].partition("/")
        gpa, scale = float(g), float(s or 4)
    nats = (
        []
        if fields.get("nationality", "").lower() in ("", "any")
        else [n.strip() for n in fields["nationality"].split(",")]
    )
    opp = {
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
        },
    }
    return "```json\n" + json.dumps({"opportunities": [opp]}) + "\n```"


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


@dataclass
class FakeLLM:
    model: str
    calls: list[list[ChatMessage]] = field(default_factory=list)
    finish_reasons: list[str | None] = field(default_factory=list)

    async def complete(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 1024, temperature: float = 0.2
    ) -> Completion:
        self.calls.append(list(messages))
        system = messages[0].content
        user = messages[-1].content
        finish: str | None = "stop"
        if "extract structured opportunity" in system:
            document = messages[1].content
            if len(document) > TRUNCATE_ABOVE_CHARS:
                # A reasoning model ran out of output tokens mid-answer.
                text, finish = '{"opportunities": [{"title": "Cut off mid', "length"
            else:
                text = _extraction_reply(user if len(messages) == 2 else document)
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

    def create(self, *, base_url: str, api_key: str, model: str) -> FakeLLM:
        llm = FakeLLM(model=model)
        self.created.append(llm)
        return llm

    @property
    def models_used(self) -> set[str]:
        return {llm.model for llm in self.created if llm.calls}

    @property
    def documents_seen(self) -> list[str]:
        return [m.content for llm in self.created for call in llm.calls for m in call]

    async def aclose(self) -> None:
        await self.http_client.aclose()
