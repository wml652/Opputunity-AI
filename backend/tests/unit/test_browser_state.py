from __future__ import annotations

import json
from datetime import UTC, datetime

from app.application.applications.portal_sessions import combine_with_checkpoint
from app.domain.applications.browser_state import merge_storage_states


def _state(*cookies: tuple[str, str], origin: str | None = None) -> str:
    return json.dumps(
        {
            "cookies": [
                {"name": n, "value": v, "domain": ".portal.edu", "path": "/"} for n, v in cookies
            ],
            "origins": [{"origin": origin, "localStorage": []}] if origin else [],
        }
    )


def _cookies(state: str | None) -> dict[str, str]:
    assert state is not None
    return {c["name"]: c["value"] for c in json.loads(state)["cookies"]}


def test_newer_state_wins_per_cookie_and_keeps_the_rest() -> None:
    merged = merge_storage_states(
        _state(("sid", "anonymous"), ("draft", "d1"), origin="https://portal.edu"),
        _state(("sid", "signed-in"), ("auth", "ok")),
    )
    assert _cookies(merged) == {"sid": "signed-in", "draft": "d1", "auth": "ok"}
    assert json.loads(merged or "")["origins"][0]["origin"] == "https://portal.edu"
    assert merge_storage_states(None, "x") == "x" and merge_storage_states("x", None) == "x"
    assert _cookies(merge_storage_states("not json", _state(("a", "1")))) == {"a": "1"}


def test_login_captured_after_a_login_wall_checkpoint_supplies_the_login_cookies() -> None:
    wall = datetime(2026, 10, 30, 9, tzinfo=UTC)
    login = datetime(2026, 10, 30, 10, tzinfo=UTC)
    checkpoint = _state(("sid", "anonymous"))
    session = _state(("sid", "signed-in"))
    assert _cookies(combine_with_checkpoint(checkpoint, wall, session, login))["sid"] == "signed-in"
    # A checkpoint taken after signing in keeps its fresher (rotated) cookies.
    later = datetime(2026, 10, 30, 11, tzinfo=UTC)
    rotated = _state(("sid", "rotated"))
    assert _cookies(combine_with_checkpoint(rotated, later, session, login))["sid"] == "rotated"
    assert combine_with_checkpoint(None, None, session, login) == session
    assert combine_with_checkpoint(checkpoint, wall, None, None) == checkpoint
