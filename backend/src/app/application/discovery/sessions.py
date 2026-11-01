"""Browser sessions (user-solved bot challenges, opt-in LinkedIn login) and search API keys.

Automation never solves a challenge or a login itself: `CaptureBrowserSession` opens a VISIBLE
browser on the user's own machine, the user passes the challenge / signs in, and only the
resulting cookies (`storage_state`) are stored, encrypted with row-bound AAD."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.domain.discovery.models import registrable_host
from app.domain.discovery.sessions import (
    BROWSER_SESSION_AAD,
    LINKEDIN_DEFAULT_DAILY_CAP,
    LINKEDIN_MAX_DAILY_CAP,
    SEARCH_CREDENTIAL_AAD,
    BrowserSession,
    SearchCredential,
    SearchProviderName,
    SessionKind,
)
from app.domain.events.models import NewEvent
from app.domain.llm.provider import last4
from app.domain.ports.crypto import ICipher, aad
from app.domain.ports.discovery import IHeadfulSessionCapturer
from app.domain.ports.hitl import HitlSession, IRemoteBrowserHub
from app.domain.ports.net import IUrlGuard
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor
from app.domain.shared.errors import NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

SESSION_TTL = timedelta(days=30)
CAPTURE_TIMEOUT_S = 300.0
PORTAL_TIMEOUT_S = 1_800.0  # signing in / registering on a portal takes time


@dataclass(frozen=True, slots=True)
class SessionDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    clock: Clock
    url_guard: IUrlGuard
    capturer: IHeadfulSessionCapturer | None  # None when no browser is installed
    spawn: Callable[[Coroutine[Any, Any, None]], None]
    # Servers: the browser runs headless and is streamed into the console (None = local window).
    hub: IRemoteBrowserHub | None = None


@dataclass(frozen=True, slots=True)
class SessionView:
    id: uuid.UUID
    domain: str
    kind: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    tos_acknowledged_at: datetime | None
    daily_action_cap: int | None
    actions_today: int

    @staticmethod
    def of(s: BrowserSession, now: datetime) -> SessionView:
        return SessionView(
            id=s.id,
            domain=s.domain,
            kind=s.kind,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
            tos_acknowledged_at=s.tos_acknowledged_at,
            daily_action_cap=s.daily_action_cap,
            actions_today=s.actions_used(now.date()),
        )


@dataclass(frozen=True, slots=True)
class ChallengedDomain:
    domain: str
    vendor: str
    url: str | None
    campaign_id: str | None
    solved: bool


@dataclass(slots=True)
class ListBrowserSessions:
    deps: SessionDeps

    async def execute(self, user_id: uuid.UUID) -> tuple[list[SessionView], list[ChallengedDomain]]:
        async with self.deps.uow_factory(user_id) as uow:
            sessions = await uow.browser_sessions.list(user_id)
            playbooks = await uow.playbooks.all_for_user(user_id)
        now = self.deps.clock.now()
        solved = {s.domain for s in sessions if s.kind == SessionKind.CHALLENGE}
        challenged = [
            ChallengedDomain(
                domain=pb.domain,
                vendor=str(pb.bot_challenge.get("vendor", "bot")),
                url=pb.bot_challenge.get("url"),
                campaign_id=pb.bot_challenge.get("campaign_id"),
                solved=pb.domain in solved,
            )
            for pb in sorted(playbooks.values(), key=lambda p: p.domain)
            if pb.bot_challenge
        ]
        return [SessionView.of(s, now) for s in sessions], challenged


@dataclass(slots=True)
class CaptureBrowserSession:
    """Starts a headful capture in the background; returns at once. Completion (or failure)
    arrives as a `session.captured` / `session.failed` event."""

    deps: SessionDeps

    async def execute(
        self,
        user_id: uuid.UUID,
        url: str,
        kind: str = SessionKind.CHALLENGE,
        *,
        tos_acknowledged: bool = False,
        daily_action_cap: int | None = None,
        on_captured: Callable[[], Awaitable[None]] | None = None,
        application_id: uuid.UUID | None = None,
    ) -> str:
        """`on_captured` runs after the cookies are stored (e.g. resume a blocked application).
        `application_id`: the application a portal sign-in is for (shown in the console)."""
        if self.deps.capturer is None:
            raise ValidationError(
                "solving challenges needs a browser: install Playwright's Chromium on this "
                "machine (or enable remote browser sessions on the server)"
            )
        if kind not in SessionKind.ALL:
            raise ValidationError("unknown session kind")
        if kind == SessionKind.LINKEDIN:
            if not tos_acknowledged:
                raise ValidationError(
                    "acknowledge LinkedIn's User Agreement first: automated activity on your "
                    "account is your responsibility and may get it restricted"
                )
            url = "https://www.linkedin.com/login"
            cap = daily_action_cap or LINKEDIN_DEFAULT_DAILY_CAP
            if not 1 <= cap <= LINKEDIN_MAX_DAILY_CAP:
                raise ValidationError(f"daily action cap must be 1-{LINKEDIN_MAX_DAILY_CAP}")
            daily_action_cap = cap
        normalized = await self.deps.url_guard.validate(url)
        domain = registrable_host(normalized) or (urlsplit(normalized).hostname or "")
        if not domain:
            raise ValidationError("URL has no host")
        hitl: HitlSession | None = None
        if self.deps.hub is not None:
            hitl = HitlSession(
                id=new_uuid7(),
                user_id=user_id,
                kind=kind,
                title=f"Sign in to {domain}" if kind != SessionKind.CHALLENGE else domain,
                url=normalized,
                application_id=application_id,
                started_at=self.deps.clock.now().isoformat(),
            )
            await self.deps.hub.open(hitl)
            await _emit(self.deps, user_id, "hitl.session_opened", hitl.to_dict())
        self.deps.spawn(
            self._capture(
                user_id,
                normalized,
                domain,
                kind,
                tos_acknowledged,
                daily_action_cap,
                on_captured,
                hitl,
            )
        )
        return domain

    async def _capture(
        self,
        user_id: uuid.UUID,
        url: str,
        domain: str,
        kind: str,
        tos_acknowledged: bool,
        daily_action_cap: int | None,
        on_captured: Callable[[], Awaitable[None]] | None = None,
        hitl: HitlSession | None = None,
    ) -> None:
        assert self.deps.capturer is not None
        try:
            state = await self.deps.capturer.capture(
                url,
                timeout_s=PORTAL_TIMEOUT_S if kind == SessionKind.PORTAL else CAPTURE_TIMEOUT_S,
                wait_for_close=kind == SessionKind.PORTAL,
                hitl=hitl,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user, never raised further
            await _emit(
                self.deps,
                user_id,
                "session.failed",
                {"domain": domain, "kind": kind, "error": str(exc)[:300]},
            )
            return
        finally:
            if hitl is not None and self.deps.hub is not None:
                await self.deps.hub.close(user_id, hitl.id)
                await _emit(self.deps, user_id, "hitl.session_closed", {"id": str(hitl.id)})
        now = self.deps.clock.now()
        async with self.deps.uow_factory(user_id) as uow:
            existing = await uow.browser_sessions.find(user_id, domain, kind)
            row_id = existing.id if existing else new_uuid7()
            session = BrowserSession(
                id=row_id,
                user_id=user_id,
                domain=domain,
                kind=kind,
                storage_state=self.deps.cipher.encrypt(
                    state.encode(), aad(BROWSER_SESSION_AAD, "storage_state", user_id, row_id)
                ),
                created_at=now,  # capture time: compared with checkpoints when merging cookies
                tos_acknowledged_at=now if tos_acknowledged else None,
                last_used_at=None,
                expires_at=now + SESSION_TTL,
                daily_action_cap=daily_action_cap,
            )
            await uow.browser_sessions.upsert(session)
            if kind == SessionKind.CHALLENGE:
                playbooks = await uow.playbooks.all_for_user(user_id)
                pb = playbooks.get(domain)
                if pb is not None and pb.bot_challenge:
                    pb.bot_challenge = None  # solved: crawl this domain again
                    pb.updated_at = now
                    await uow.playbooks.save(pb)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="browser_session.captured",
                entity="browser_sessions",
                entity_id=row_id,
                metadata={"domain": domain, "kind": kind},
            )
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="session.captured",
                    payload={"domain": domain, "kind": kind},
                )
            )
            await uow.commit()
        if on_captured is not None:
            try:
                await on_captured()
            except Exception as exc:  # noqa: BLE001 - reported to the user, never raised further
                await _emit(
                    self.deps,
                    user_id,
                    "session.failed",
                    {"domain": domain, "kind": kind, "error": f"saved, but: {str(exc)[:250]}"},
                )


@dataclass(slots=True)
class DeleteBrowserSession:
    deps: SessionDeps

    async def execute(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            if not await uow.browser_sessions.delete(user_id, session_id):
                raise NotFoundError("session not found")
            await uow.commit()


# ---- search credentials -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchCredentialView:
    provider: str
    key_last4: str
    updated_at: datetime


@dataclass(slots=True)
class ListSearchCredentials:
    deps: SessionDeps

    async def execute(self, user_id: uuid.UUID) -> list[SearchCredentialView]:
        async with self.deps.uow_factory(user_id) as uow:
            creds = await uow.search_credentials.list(user_id)
        return [SearchCredentialView(c.provider, c.key_last4, c.updated_at) for c in creds]


@dataclass(slots=True)
class SaveSearchCredential:
    deps: SessionDeps

    async def execute(self, user_id: uuid.UUID, provider: str, key: str) -> SearchCredentialView:
        provider = provider.strip().lower()
        if provider not in SearchProviderName.ALL:
            raise ValidationError(f"provider must be one of {', '.join(SearchProviderName.ALL)}")
        key = key.strip()
        if len(key) < 8:
            raise ValidationError("that does not look like an API key")
        now = self.deps.clock.now()
        row_id = new_uuid7()  # a new row id (and AAD) for every saved key
        cred = SearchCredential(
            id=row_id,
            user_id=user_id,
            provider=provider,
            key=self.deps.cipher.encrypt(
                key.encode(), aad(SEARCH_CREDENTIAL_AAD, "key", user_id, row_id)
            ),
            key_last4=last4(key),
            updated_at=now,
        )
        async with self.deps.uow_factory(user_id) as uow:
            await uow.search_credentials.upsert(cred)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="search_credential.saved",
                entity="search_credentials",
                entity_id=row_id,
                metadata={"provider": provider},
            )
            await uow.commit()
        return SearchCredentialView(provider, cred.key_last4, now)


@dataclass(slots=True)
class DeleteSearchCredential:
    deps: SessionDeps

    async def execute(self, user_id: uuid.UUID, provider: str) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            if not await uow.search_credentials.delete(user_id, provider.strip().lower()):
                raise NotFoundError("no key saved for that provider")
            await uow.commit()


async def _emit(
    deps: SessionDeps, user_id: uuid.UUID, type_: str, payload: dict[str, object]
) -> None:
    async with deps.uow_factory(user_id) as uow:
        await uow.events.append(NewEvent(user_id=user_id, type=type_, payload=dict(payload)))
        await uow.commit()
