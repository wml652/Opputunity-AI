from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from app.domain.identity.models import (
    IssuedSession,
    RefreshToken,
    User,
    normalize_email,
    validate_password,
)
from app.domain.ports.security import (
    IAccessTokenService,
    IPasswordHasher,
    IRefreshTokenCodec,
    RefreshTokenParts,
)
from app.domain.ports.uow import IUnitOfWork, IUnitOfWorkFactory
from app.domain.shared.clock import Clock
from app.domain.shared.enums import Actor
from app.domain.shared.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    TokenReuseError,
)
from app.domain.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class AuthDeps:
    uow_factory: IUnitOfWorkFactory
    hasher: IPasswordHasher
    access_tokens: IAccessTokenService
    refresh_codec: IRefreshTokenCodec
    clock: Clock
    refresh_ttl: timedelta


async def _issue_session(
    deps: AuthDeps, uow: IUnitOfWork, user: User, family_id: uuid.UUID
) -> tuple[IssuedSession, RefreshToken]:
    now = deps.clock.now()
    secret = deps.refresh_codec.generate_secret()
    row = RefreshToken(
        id=new_uuid7(),
        user_id=user.id,
        family_id=family_id,
        token_hash=deps.refresh_codec.hash_secret(secret),
        expires_at=now + deps.refresh_ttl,
        revoked_at=None,
        replaced_by=None,
        created_at=now,
    )
    await uow.refresh_tokens.add(row)
    access = deps.access_tokens.issue(user.id, now)
    raw_refresh = deps.refresh_codec.encode(
        RefreshTokenParts(user_id=user.id, token_id=row.id, secret=secret)
    )
    session = IssuedSession(
        user=user,
        access_token=access.token,
        access_expires_at=access.expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=row.expires_at,
    )
    return session, row


@dataclass(slots=True)
class RegisterUser:
    deps: AuthDeps

    async def execute(self, *, email: str, password: str, display_name: str) -> IssuedSession:
        normalized = normalize_email(email)
        validate_password(password)
        now = self.deps.clock.now()
        user = User(
            id=new_uuid7(),
            email=normalized,
            password_hash=self.deps.hasher.hash(password),
            display_name=display_name.strip()[:120] or normalized.split("@")[0],
            timezone="UTC",
            is_active=True,
            created_at=now,
        )
        async with self.deps.uow_factory() as uow:
            if await uow.users.get_by_email(normalized) is not None:
                raise ConflictError("an account with this email already exists")
            await uow.users.add(user)
            await uow.bind_user(user.id)
            session, _ = await _issue_session(self.deps, uow, user, family_id=new_uuid7())
            await uow.audit.record(
                user_id=user.id,
                actor=Actor.USER,
                action="auth.register",
                entity="user",
                entity_id=user.id,
            )
            await uow.commit()
        return session


# Hash of a random password; used to keep login timing uniform for unknown emails.
_DUMMY_PASSWORD = "dummy-password-for-timing-equalization"  # noqa: S105


@dataclass(slots=True)
class LoginUser:
    deps: AuthDeps
    _dummy_hash: str | None = None

    async def execute(self, *, email: str, password: str, ip: str | None) -> IssuedSession:
        try:
            normalized = normalize_email(email)
        except Exception as exc:
            raise AuthenticationError("invalid email or password") from exc
        async with self.deps.uow_factory() as uow:
            user = await uow.users.get_by_email(normalized)
            if user is None or not user.is_active:
                if self._dummy_hash is None:
                    self._dummy_hash = self.deps.hasher.hash(_DUMMY_PASSWORD)
                self.deps.hasher.verify(self._dummy_hash, password)
                raise AuthenticationError("invalid email or password")
            await uow.bind_user(user.id)
            if not self.deps.hasher.verify(user.password_hash, password):
                await uow.audit.record(
                    user_id=user.id,
                    actor=Actor.USER,
                    action="auth.login_failed",
                    entity="user",
                    entity_id=user.id,
                    ip=ip,
                )
                await uow.commit()
                raise AuthenticationError("invalid email or password")
            if self.deps.hasher.needs_rehash(user.password_hash):
                await uow.users.update_password_hash(user.id, self.deps.hasher.hash(password))
            session, _ = await _issue_session(self.deps, uow, user, family_id=new_uuid7())
            await uow.audit.record(
                user_id=user.id,
                actor=Actor.USER,
                action="auth.login",
                entity="user",
                entity_id=user.id,
                ip=ip,
            )
            await uow.commit()
        return session


@dataclass(slots=True)
class RefreshSession:
    """Rotate a refresh token. Presenting an already-rotated token revokes the whole family."""

    deps: AuthDeps

    async def execute(self, *, raw_refresh_token: str, ip: str | None) -> IssuedSession:
        try:
            parts = self.deps.refresh_codec.decode(raw_refresh_token)
        except ValueError as exc:
            raise AuthenticationError("invalid refresh token") from exc
        now = self.deps.clock.now()
        async with self.deps.uow_factory(parts.user_id) as uow:
            row = await uow.refresh_tokens.get(parts.user_id, parts.token_id)
            if row is None or not self.deps.refresh_codec.secrets_match(
                parts.secret, row.token_hash
            ):
                raise AuthenticationError("invalid refresh token")
            if row.revoked_at is not None:
                revoked = await uow.refresh_tokens.revoke_family(parts.user_id, row.family_id, now)
                await uow.audit.record(
                    user_id=parts.user_id,
                    actor=Actor.SYSTEM,
                    action="auth.refresh_reuse_detected",
                    entity="refresh_token_family",
                    entity_id=row.family_id,
                    ip=ip,
                    metadata={"revoked": revoked},
                )
                await uow.commit()
                raise TokenReuseError("refresh token reuse detected; session family revoked")
            if row.expires_at <= now:
                raise AuthenticationError("refresh token expired")
            user = await uow.users.get(parts.user_id)
            if user is None or not user.is_active:
                raise AuthenticationError("account disabled")
            session, new_row = await _issue_session(self.deps, uow, user, row.family_id)
            await uow.refresh_tokens.revoke(parts.user_id, row.id, now, replaced_by=new_row.id)
            await uow.commit()
        return session


@dataclass(slots=True)
class Logout:
    deps: AuthDeps

    async def execute(self, *, raw_refresh_token: str | None) -> None:
        if not raw_refresh_token:
            return
        try:
            parts = self.deps.refresh_codec.decode(raw_refresh_token)
        except ValueError:
            return
        async with self.deps.uow_factory(parts.user_id) as uow:
            row = await uow.refresh_tokens.get(parts.user_id, parts.token_id)
            if row is None or not self.deps.refresh_codec.secrets_match(
                parts.secret, row.token_hash
            ):
                return
            await uow.refresh_tokens.revoke_family(
                parts.user_id, row.family_id, self.deps.clock.now()
            )
            await uow.audit.record(
                user_id=parts.user_id,
                actor=Actor.USER,
                action="auth.logout",
                entity="refresh_token_family",
                entity_id=row.family_id,
            )
            await uow.commit()


DEV_USER_EMAIL = "dev@opportunity.local"


@dataclass(slots=True)
class EnsureDevUser:
    """DEV_AUTH_BYPASS support: find or create the local dev user (random, unusable password)."""

    deps: AuthDeps

    async def execute(self) -> uuid.UUID:
        async with self.deps.uow_factory() as uow:
            existing = await uow.users.get_by_email(DEV_USER_EMAIL)
            if existing is not None:
                return existing.id
            user = User(
                id=new_uuid7(),
                email=DEV_USER_EMAIL,
                password_hash=self.deps.hasher.hash(self.deps.refresh_codec.generate_secret()),
                display_name="Dev User",
                timezone="UTC",
                is_active=True,
                created_at=self.deps.clock.now(),
            )
            await uow.users.add(user)
            await uow.bind_user(user.id)
            await uow.audit.record(
                user_id=user.id,
                actor=Actor.SYSTEM,
                action="auth.dev_user_created",
                entity="user",
                entity_id=user.id,
            )
            await uow.commit()
            return user.id


@dataclass(slots=True)
class GetCurrentUser:
    uow_factory: IUnitOfWorkFactory

    async def execute(self, user_id: uuid.UUID) -> User:
        async with self.uow_factory(user_id) as uow:
            user = await uow.users.get(user_id)
        if user is None or not user.is_active:
            raise NotFoundError("user not found")
        return user
