"""Milestone 10 units: PII tokenization, DOM signatures and the SFT / DPO record builders."""

from __future__ import annotations

import json
import uuid

from app.domain.training.models import (
    DomState,
    Trajectory,
    TrajectorySource,
    click_action,
    dpo_records,
    fill_action,
    sft_record,
    synthetic_negative,
)
from app.domain.training.privacy import leaks, profile_token, tokenize_pii

KNOWN = {
    "full_name": "Zara Siddiqui",
    "first_name": "Zara",
    "email": "zara@example.com",
    "phone": "+92 300 5550199",
    "city": "Lahore",
    "referee_1_name": "Dr. Amina Khan",
}


def test_tokenization_replaces_every_personal_value_with_its_profile_token() -> None:
    text = (
        "Applicant Zara Siddiqui (zara@example.com, +92 300 5550199) from Lahore; referee "
        "Dr. Amina Khan; other contact b@c.org / 0300-1234567 / https://linkedin.com/in/x"
    )
    out = tokenize_pii(text, KNOWN)
    assert "{{PROFILE.FULL_NAME}}" in out and "{{PROFILE.EMAIL}}" in out
    assert "{{PROFILE.PHONE}}" in out and "{{PROFILE.CITY}}" in out
    assert "{{PROFILE.REFEREE_1_NAME}}" in out
    assert "{{PII.EMAIL}}" in out and "{{PII.PHONE}}" in out and "{{PII.URL}}" in out
    assert leaks(out, KNOWN) == []
    assert leaks("hello Zara", KNOWN) == ["first_name"]
    assert profile_token("phone") == "{{PROFILE.PHONE}}"
    assert profile_token("gpa") == "{{PROFILE.GPA}}"


def test_dom_signature_is_stable_and_bounded() -> None:
    state = DomState.of(
        "https://portal.edu/apply/step1?x=1",
        "  Apply  ",
        ["Step 1"],
        ["Next", "Next", " Save draft "],
        ["Full name"],
    )
    assert state.path == "/apply/step1"
    assert state.buttons == ("Next", "Save draft")  # de-duplicated, whitespace-normalised
    assert state.signature().splitlines()[0] == "path: /apply/step1"


def _t(action: dict, rejected: dict | None = None, source=TrajectorySource.TEACH) -> Trajectory:
    sig = DomState.of(
        "https://p.edu/start", "Programme", [], ["Subscribe to newsletter", "Apply online"], []
    ).signature()
    return Trajectory(uuid.uuid4(), uuid.uuid4(), source, "p.edu", sig, action, rejected)


def test_sft_and_dpo_records() -> None:
    click = _t(click_action("Apply online"))
    record = sft_record(click)
    roles = [m["role"] for m in record["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert json.loads(record["messages"][2]["content"])["label"] == "Apply online"
    # a decoy control is the synthetic hard negative for a taught click
    assert synthetic_negative(click) == click_action("Subscribe to newsletter", "button")
    fill = _t(fill_action("Mobile number", "{{PROFILE.PHONE}}"))
    assert synthetic_negative(fill)["value_token"] != "{{PROFILE.PHONE}}"  # type: ignore[index]
    corrected = _t(click_action("Apply online"), rejected={"action": "give_up", "label": ""})
    auto = _t(click_action("Apply online"), source=TrajectorySource.AUTO_SUCCESS)
    rows = dpo_records([click, fill, corrected, auto])
    assert len(rows) == 3  # auto successes without a correction carry no preference
    real = [r for r in rows if not r["metadata"]["synthetic"]]
    assert len(real) == 1 and json.loads(real[0]["rejected"])["action"] == "give_up"
