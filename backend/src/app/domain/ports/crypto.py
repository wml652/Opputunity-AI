from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.llm.provider import EncryptedSecret


def aad(table: str, column: str, user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    """Row-bound associated data: a ciphertext cannot be moved to another row/column/tenant."""
    return f"{table}:{column}:{user_id}:{row_id}".encode()


class ICipher(Protocol):
    @property
    def active_key_version(self) -> int: ...

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> EncryptedSecret: ...

    def decrypt(self, value: EncryptedSecret, associated_data: bytes) -> bytes: ...

    def reencrypt(self, value: EncryptedSecret, associated_data: bytes) -> EncryptedSecret: ...
