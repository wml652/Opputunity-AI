from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.domain.ports.security import RefreshTokenParts
from app.domain.shared.errors import AuthenticationError
from app.domain.shared.ids import new_uuid7
from app.infrastructure.security.passwords import Argon2PasswordHasher
from app.infrastructure.security.tokens import JwtAccessTokenService, RefreshTokenCodec

SECRET = "unit-test-secret-that-is-at-least-32-chars"


def _svc(ttl: timedelta = timedelta(minutes=15)) -> JwtAccessTokenService:
    return JwtAccessTokenService(secret=SECRET, issuer="test", ttl=ttl)


def test_access_token_round_trip() -> None:
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    token = _svc().issue(uid, now)
    claims = _svc().verify(token.token)
    assert claims.user_id == uid
    assert abs((claims.expires_at - (now + timedelta(minutes=15))).total_seconds()) < 2


def test_expired_access_token_rejected() -> None:
    token = _svc(timedelta(seconds=-5)).issue(uuid.uuid4(), datetime.now(UTC))
    with pytest.raises(AuthenticationError):
        _svc().verify(token.token)


def test_wrong_signature_rejected() -> None:
    token = JwtAccessTokenService(secret="x" * 40, issuer="test", ttl=timedelta(minutes=5)).issue(
        uuid.uuid4(), datetime.now(UTC)
    )
    with pytest.raises(AuthenticationError):
        _svc().verify(token.token)


def test_non_access_token_type_rejected() -> None:
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": "test",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "typ": "refresh",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        _svc().verify(forged)


def test_alg_none_rejected() -> None:
    forged = jwt.encode({"sub": str(uuid.uuid4()), "iss": "test"}, key="", algorithm="none")
    with pytest.raises(AuthenticationError):
        _svc().verify(forged)


def test_short_secret_refused() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        JwtAccessTokenService(secret="short", issuer="t", ttl=timedelta(minutes=1))


def test_refresh_codec() -> None:
    codec = RefreshTokenCodec()
    secret = codec.generate_secret()
    parts = RefreshTokenParts(user_id=uuid.uuid4(), token_id=new_uuid7(), secret=secret)
    raw = codec.encode(parts)
    assert codec.decode(raw) == parts
    stored = codec.hash_secret(secret)
    assert secret not in stored
    assert codec.secrets_match(secret, stored)
    assert not codec.secrets_match(secret + "x", stored)
    for bad in ("", "a.b", "not-a-uuid.also-not.secret", f"{uuid.uuid4()}.{uuid.uuid4()}."):
        with pytest.raises(ValueError):  # noqa: PT011
            codec.decode(bad)


def test_argon2id_hasher() -> None:
    hasher = Argon2PasswordHasher(time_cost=1, memory_cost_kib=1024, parallelism=1)
    digest = hasher.hash("correct-horse-battery")
    assert digest.startswith("$argon2id$")
    assert hasher.verify(digest, "correct-horse-battery")
    assert not hasher.verify(digest, "wrong-password")
    assert not hasher.verify("not-a-hash", "x")
    stronger = Argon2PasswordHasher(time_cost=2, memory_cost_kib=2048, parallelism=1)
    assert stronger.needs_rehash(digest)


def test_uuid7_is_versioned_and_time_ordered() -> None:
    ids = [new_uuid7() for _ in range(200)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert len(set(ids)) == 200
    # 48-bit millisecond prefix is monotonic non-decreasing
    prefixes = [i.int >> 80 for i in ids]
    assert prefixes == sorted(prefixes)
