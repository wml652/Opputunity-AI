"""Saved browser sessions for application portals.

When the user signed in to a portal (`SessionKind.PORTAL`) or passed its bot challenge
(`SessionKind.CHALLENGE`) in a visible browser, fill and submit runs start from those encrypted
cookies so the filler is authenticated - automation never types a password itself."""

from __future__ import annotations

import uuid
from datetime import datetime
from urllib.parse import urlsplit

from app.domain.applications.browser_state import merge_storage_states
from app.domain.discovery.models import registrable_host
from app.domain.discovery.sessions import BROWSER_SESSION_AAD, SessionKind
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.uow import IUnitOfWorkFactory


def portal_domain(url: str) -> str:
    return registrable_host(url) or (urlsplit(url).hostname or "")


async def saved_portal_state(
    uow_factory: IUnitOfWorkFactory,
    cipher: ICipher,
    now: datetime,
    user_id: uuid.UUID,
    portal_url: str,
    *also: str,
) -> tuple[str | None, datetime | None]:
    """(storage_state JSON, capture time) of the unexpired PORTAL + CHALLENGE sessions for the
    portal's domain and for `also` URLs (e.g. the official sign-in page a checkpoint stopped
    at); a PORTAL login overrides CHALLENGE cookies. (None, None) if there is none."""
    domains = list(dict.fromkeys(d for d in map(portal_domain, (portal_url, *also)) if d))
    if not domains:
        return None, None
    async with uow_factory(user_id) as uow:
        found = [
            await uow.browser_sessions.find(user_id, domain, kind)
            for domain in domains
            for kind in (SessionKind.CHALLENGE, SessionKind.PORTAL)
        ]
    state: str | None = None
    captured: datetime | None = None
    for session in found:
        if session is None or session.is_expired(now):
            continue
        plain = cipher.decrypt(
            session.storage_state,
            aad(BROWSER_SESSION_AAD, "storage_state", user_id, session.id),
        ).decode()
        state = merge_storage_states(state, plain)
        captured = max(captured, session.created_at) if captured else session.created_at
    return state, captured


def combine_with_checkpoint(
    checkpoint_state: str | None,
    checkpoint_at: datetime | None,
    session_state: str | None,
    session_at: datetime | None,
) -> str | None:
    """The newer of the two wins per cookie: a session captured after a login-wall checkpoint
    supplies the login cookies; a checkpoint taken after signing in keeps its fresher ones."""
    if checkpoint_state is None or session_state is None:
        return checkpoint_state or session_state
    if session_at is not None and checkpoint_at is not None and session_at < checkpoint_at:
        return merge_storage_states(session_state, checkpoint_state)
    return merge_storage_states(checkpoint_state, session_state)
