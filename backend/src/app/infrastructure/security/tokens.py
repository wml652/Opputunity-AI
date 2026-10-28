from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.domain.ports.security import AccessClaims, AccessToken, RefreshTokenParts
from app.domain.shared.errors import AuthenticationError
from app.domain.shared.ids import new_uuid7

_ALGORITHM = "HS256"


class JwtAccessTokenService:
    def __init__(self, *, secret: str, issuer: str, ttl: timedelta) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self._secret = secret
        self._issuer = issuer
        self._ttl = ttl

    def issue(self, user_id: uuid.UUID, now: datetime) -> AccessToken:
        expires = now + self._ttl
        token = jwt.encode(
            {
                "sub": str(user_id),
                "iss": self._issuer,
                "iat": int(now.timestamp()),
                "exp": int(expires.timestamp()),
                "jti": str(new_uuid7()),
                "typ": "access",
            },
            self._secret,
            algorithm=_ALGORITHM,
        )
        return AccessToken(token=token, expires_at=expires)

    def verify(self, token: str) -> AccessClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                issuer=self._issuer,
                options={"require": ["sub", "exp", "iat", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("invalid or expired access token") from exc
        if claims.get("typ") != "access":
            raise AuthenticationError("wrong token type")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise AuthenticationError("invalid subject") from exc
        return AccessClaims(
            user_id=user_id, expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
        )


class RefreshTokenCodec:
    """`<user_id>.<token_id>.<secret>`. The user id lets the server set the RLS tenant before the
    lookup; only SHA-256(secret) is stored, compared in constant time."""

    def generate_secret(self) -> str:
        return secrets.token_urlsafe(32)

    def encode(self, parts: RefreshTokenParts) -> str:
        return f"{parts.user_id}.{parts.token_id}.{parts.secret}"

    def decode(self, token: str) -> RefreshTokenParts:
        pieces = token.split(".")
        if len(pieces) != 3 or not pieces[2]:
            raise ValueError("malformed refresh token")
        return RefreshTokenParts(
            user_id=uuid.UUID(pieces[0]), token_id=uuid.UUID(pieces[1]), secret=pieces[2]
        )

    def hash_secret(self, secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    def secrets_match(self, secret: str, token_hash: str) -> bool:
        return hmac.compare_digest(self.hash_secret(secret), token_hash)
