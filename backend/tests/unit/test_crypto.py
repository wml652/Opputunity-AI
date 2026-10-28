from __future__ import annotations

import base64
import uuid
from dataclasses import replace

import pytest

from app.domain.ports.crypto import aad
from app.domain.shared.errors import DecryptionError
from app.infrastructure.crypto.aes_gcm_cipher import AesGcmCipher
from app.infrastructure.crypto.key_ring import KeyRing
from app.infrastructure.crypto.keygen import new_key_entry

K1 = b"\x01" * 32
K2 = b"\x02" * 32
USER = uuid.uuid4()
ROW = uuid.uuid4()
AAD = aad("llm_provider_configs", "api_key", USER, ROW)
SECRET = b"nvapi-THIS-IS-A-SECRET-KEY-123456"


@pytest.fixture
def cipher() -> AesGcmCipher:
    return AesGcmCipher(KeyRing({1: K1}))


def test_round_trip(cipher: AesGcmCipher) -> None:
    enc = cipher.encrypt(SECRET, AAD)
    assert enc.key_version == 1
    assert len(enc.nonce) == 12
    assert SECRET not in enc.ciphertext
    assert cipher.decrypt(enc, AAD) == SECRET


def test_nonce_is_random_per_encryption(cipher: AesGcmCipher) -> None:
    a, b = cipher.encrypt(SECRET, AAD), cipher.encrypt(SECRET, AAD)
    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext


def test_tampered_ciphertext_is_rejected(cipher: AesGcmCipher) -> None:
    enc = cipher.encrypt(SECRET, AAD)
    flipped = bytes([enc.ciphertext[0] ^ 0x01]) + enc.ciphertext[1:]
    with pytest.raises(DecryptionError):
        cipher.decrypt(replace(enc, ciphertext=flipped), AAD)


def test_tampered_nonce_is_rejected(cipher: AesGcmCipher) -> None:
    enc = cipher.encrypt(SECRET, AAD)
    with pytest.raises(DecryptionError):
        cipher.decrypt(replace(enc, nonce=b"\x00" * 12), AAD)
    with pytest.raises(DecryptionError):
        cipher.decrypt(replace(enc, nonce=b"short"), AAD)


@pytest.mark.parametrize(
    "other_aad",
    [
        aad("llm_provider_configs", "api_key", uuid.uuid4(), ROW),  # another tenant
        aad("llm_provider_configs", "api_key", USER, uuid.uuid4()),  # another row
        aad("llm_provider_configs", "embedding_api_key", USER, ROW),  # another column
        aad("browser_sessions", "api_key", USER, ROW),  # another table
    ],
)
def test_aad_mismatch_is_rejected(cipher: AesGcmCipher, other_aad: bytes) -> None:
    enc = cipher.encrypt(SECRET, AAD)
    with pytest.raises(DecryptionError):
        cipher.decrypt(enc, other_aad)


def test_key_rotation() -> None:
    old = AesGcmCipher(KeyRing({1: K1}))
    legacy = old.encrypt(SECRET, AAD)

    rotated = AesGcmCipher(KeyRing({1: K1, 2: K2}))
    assert rotated.active_key_version == 2
    assert rotated.decrypt(legacy, AAD) == SECRET  # old versions still decrypt

    fresh = rotated.encrypt(SECRET, AAD)
    assert fresh.key_version == 2

    migrated = rotated.reencrypt(legacy, AAD)
    assert migrated.key_version == 2
    assert rotated.reencrypt(migrated, AAD) is migrated  # already current: no-op

    retired = AesGcmCipher(KeyRing({2: K2}))  # v1 retired after background re-encryption
    assert retired.decrypt(migrated, AAD) == SECRET
    with pytest.raises(DecryptionError):
        retired.decrypt(legacy, AAD)


def test_wrong_key_same_version_is_rejected() -> None:
    enc = AesGcmCipher(KeyRing({1: K1})).encrypt(SECRET, AAD)
    with pytest.raises(DecryptionError):
        AesGcmCipher(KeyRing({1: K2})).decrypt(enc, AAD)


def test_key_ring_from_env() -> None:
    spec = f"v2:{base64.b64encode(K2).decode()}, 1:{base64.b64encode(K1).decode()}"
    ring = KeyRing.from_env(spec)
    assert ring.active_version == 2
    assert ring.versions == [1, 2]
    assert ring.key(1) == K1


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "no-colon",
        f"1:{base64.b64encode(b'short').decode()}",
        "1:!!!not-base64!!!",
        f"1:{base64.b64encode(K1).decode()},1:{base64.b64encode(K2).decode()}",
        f"0:{base64.b64encode(K1).decode()}",
    ],
)
def test_key_ring_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        KeyRing.from_env(spec)


def test_keygen_entry_is_loadable() -> None:
    ring = KeyRing.from_env(new_key_entry(3))
    assert ring.active_version == 3
