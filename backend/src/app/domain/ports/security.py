from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class IPasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    expires_at: datetime


class IAccessTokenService(Protocol):
    def issue(self, user_id: uuid.UUID, now: datetime) -> AccessToken: ...

    def verify(self, token: str) -> AccessClaims: ...


@dataclass(frozen=True, slots=True)
class RefreshTokenParts:
    user_id: uuid.UUID
    token_id: uuid.UUID
    secret: str


class IRefreshTokenCodec(Protocol):
    """Opaque refresh token format `<user_id>.<token_id>.<secret>`; only a hash is stored."""

    def generate_secret(self) -> str: ...

    def encode(self, parts: RefreshTokenParts) -> str: ...

    def decode(self, token: str) -> RefreshTokenParts: ...

    def hash_secret(self, secret: str) -> str: ...

    def secrets_match(self, secret: str, token_hash: str) -> bool: ...
