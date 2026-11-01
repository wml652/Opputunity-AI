"""Milestone 9 units: model catalog categories, reasoning-model output handling, NIM error hints,
the self-submit transition and the Teach Mode learning helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.agent.models import (
    Locator,
    StepKind,
    TeachStep,
    detect_ats,
    final_field_values,
    looks_submitted,
    navigation_path,
)
from app.domain.applications.models import Application, IllegalTransitionError, advance
from app.domain.llm.catalog import build_catalog, categorize_model
from app.domain.shared.enums import Actor, ApplicationState
from app.infrastructure.extraction.llm_adapters import parse_json_object
from app.infrastructure.llm.http_client import _hint
from app.infrastructure.llm.openai_compatible import visible_answer

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def test_catalog_categories_are_name_heuristics_only() -> None:
    cases = {
        "acme/chat-large-70b-instruct": "chat",
        "acme/tiny-8b-instruct": "fast",
        "acme/vision-90b-instruct": "vision",
        "acme/omni-super-vl-120b": "vision",
        "acme/reasoner-r1": "reasoning",
        "acme/big-super-49b": "reasoning",
        "acme/nv-embedqa-e5-v5": "embedding",
        "acme/nv-rerankqa-1b": "rerank",
        "acme/model-340b-reward": "other",
        "acme/content-safety-8b": "other",
    }
    for model_id, category in cases.items():
        assert categorize_model(model_id) == category, model_id
    catalog = build_catalog([*cases, "acme/chat-large-70b-instruct", " "])
    assert len(catalog) == len(cases)  # de-duplicated, blanks dropped
    assert catalog[0].category == "chat" and catalog[-1].category == "other"
    assert not next(i for i in catalog if i.category == "other").chat_capable


def test_reasoning_output_is_reduced_to_the_answer() -> None:
    assert visible_answer('<think>plan...</think>{"ok": true}') == '{"ok": true}'
    assert visible_answer("<think>never closed {not json}") == ""
    # Everything in reasoning_content, nothing in content: the last JSON object there wins.
    reasoning = 'First {"draft": 1} then the final answer {"ok": true, "n": {"a": 2}}'
    assert visible_answer("", reasoning) == '{"ok": true, "n": {"a": 2}}'
    assert visible_answer("plain answer", reasoning) == "plain answer"
    assert parse_json_object('<think>x {"no": 1}</think>\n```json\n{"ok": true}\n```') == {
        "ok": True
    }


def test_nim_errors_get_actionable_hints() -> None:
    assert "not enabled for your key" in _hint(404, "Function 'abc': Not found for account 'x'")
    assert "retired" in _hint(404, "model not found")
    assert "reward or rerank" in _hint(422, "unprocessable")
    assert "Requests per minute" in _hint(429, "too many requests")
    assert _hint(500, "boom") == ""


def _app(state: ApplicationState) -> Application:
    return Application(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), state, 1, None, NOW, NOW)


def test_only_the_user_can_record_a_self_submission() -> None:
    app = _app(ApplicationState.BLOCKED_NEEDS_HUMAN)
    advance(app, ApplicationState.SUBMITTED, actor=Actor.USER, reason="teach", at=NOW)
    assert app.state is ApplicationState.SUBMITTED
    for actor in (Actor.WORKER, Actor.SYSTEM):
        with pytest.raises(IllegalTransitionError):
            advance(
                _app(ApplicationState.QUEUED),
                ApplicationState.SUBMITTED,
                actor=actor,
                reason="x",
                at=NOW,
            )
    with pytest.raises(IllegalTransitionError):  # not while the agent is mid-fill
        advance(
            _app(ApplicationState.FILLING),
            ApplicationState.SUBMITTED,
            actor=Actor.USER,
            reason="x",
            at=NOW,
        )


def test_teach_helpers_extract_route_answers_and_confirmation() -> None:
    def click(name: str) -> TeachStep:
        return TeachStep(StepKind.CLICK, "https://p.edu/a", Locator(role="button", name=name))

    steps = [
        TeachStep(StepKind.NAVIGATE, "https://p.edu/"),
        click("Accept all"),
        click("Apply online"),
        TeachStep(StepKind.NAVIGATE, "https://p.edu/form"),
        TeachStep(
            StepKind.FILL, "https://p.edu/form", field_label="Name", field_name="n", value="Z"
        ),
        TeachStep(
            StepKind.FILL, "https://p.edu/form", field_label="Name", field_name="n", value="Zara"
        ),
        TeachStep.from_dict(
            {
                "type": "fill",
                "url": "u",
                "field_label": "Password",
                "field_type": "password",
                "value": "hunter2",
            }
        )
        or click("x"),
        click("Submit"),
    ]
    path = navigation_path(steps)
    assert [s.locator.name or s.url for s in path] == [
        "https://p.edu/",
        "Accept all",
        "Apply online",
    ]
    finals = {s.field_label: s for s in final_field_values(steps)}
    assert finals["Name"].value == "Zara"
    assert finals["Password"].value is None and finals["Password"].secret  # never recorded
    assert looks_submitted("Thank you for your application. It has been received.", steps)
    assert not looks_submitted("Please review your answers", steps)
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/x").value == "workday"
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1").value == "greenhouse"
    assert detect_ats("https://www.example.edu/apply").value == "generic_portal"
