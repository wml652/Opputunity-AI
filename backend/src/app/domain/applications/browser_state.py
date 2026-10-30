"""Combining Playwright `storage_state` JSON documents (cookies + per-origin localStorage).

A fill run can have two sources of browser state: the application's own checkpoint and a
session the user captured by signing in / passing a challenge in a visible browser. A checkpoint
taken at a login wall has no login cookies, so the newer document's cookies and origins win and
the older one only contributes what the newer one lacks."""

from __future__ import annotations

import json
from typing import Any


def _load(state: str | None) -> dict[str, Any]:
    if not state:
        return {"cookies": [], "origins": []}
    try:
        data = json.loads(state)
    except ValueError:
        return {"cookies": [], "origins": []}
    if not isinstance(data, dict):
        return {"cookies": [], "origins": []}
    cookies = data.get("cookies")
    origins = data.get("origins")
    return {
        "cookies": [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else [],
        "origins": [o for o in origins if isinstance(o, dict)] if isinstance(origins, list) else [],
    }


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name", "")),
        str(cookie.get("domain", "")).lstrip(".").lower(),
        str(cookie.get("path", "/")),
    )


def merge_storage_states(older: str | None, newer: str | None) -> str | None:
    """`newer` overrides `older` per cookie (name, domain, path) and per localStorage origin."""
    if not older:
        return newer
    if not newer:
        return older
    base, top = _load(older), _load(newer)
    cookies = {_cookie_key(c): c for c in base["cookies"]}
    cookies.update({_cookie_key(c): c for c in top["cookies"]})
    origins = {str(o.get("origin", "")): o for o in base["origins"]}
    origins.update({str(o.get("origin", "")): o for o in top["origins"]})
    return json.dumps({"cookies": list(cookies.values()), "origins": list(origins.values())})
