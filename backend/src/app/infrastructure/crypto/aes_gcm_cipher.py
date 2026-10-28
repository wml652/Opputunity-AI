from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.llm.provider import EncryptedSecret
from app.domain.shared.errors import DecryptionError
from app.infrastructure.crypto.key_ring import KeyRing

NONCE_BYTES = 12  # 96-bit nonce, the GCM standard; random per encryption


class AesGcmCipher:
    """AES-256-GCM with caller-supplied associated data (row-bound AAD, see `ports.crypto.aad`).
    Tampering with ciphertext, nonce, AAD, or key version raises `DecryptionError`."""

    def __init__(self, key_ring: KeyRing) -> None:
        self._ring = key_ring

    @property
    def active_key_version(self) -> int:
        return self._ring.active_version

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> EncryptedSecret:
        version = self._ring.active_version
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._ring.key(version)).encrypt(nonce, plaintext, associated_data)
        return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, key_version=version)

    def decrypt(self, value: EncryptedSecret, associated_data: bytes) -> bytes:
        if len(value.nonce) != NONCE_BYTES:
            raise DecryptionError("invalid nonce length")
        try:
            key = self._ring.key(value.key_version)
        except KeyError as exc:
            raise DecryptionError(str(exc)) from None
        try:
            return AESGCM(key).decrypt(value.nonce, value.ciphertext, associated_data)
        except InvalidTag:
            raise DecryptionError("ciphertext authentication failed") from None

    def reencrypt(self, value: EncryptedSecret, associated_data: bytes) -> EncryptedSecret:
        """Rotate to the active key; returns the input unchanged if already current."""
        if value.key_version == self._ring.active_version:
            return value
        return self.encrypt(self.decrypt(value, associated_data), associated_data)
