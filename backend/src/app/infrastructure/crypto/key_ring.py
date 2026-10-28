from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

KEY_BYTES = 32  # AES-256


class KeyRing:
    """Versioned AES-256 keys. The highest version encrypts; every version can decrypt, so keys
    rotate by adding a new version, re-encrypting rows in the background, then retiring the old."""

    def __init__(self, keys: Mapping[int, bytes]) -> None:
        if not keys:
            raise ValueError("KeyRing requires at least one key")
        for version, key in keys.items():
            if version < 1:
                raise ValueError("key versions must be positive integers")
            if len(key) != KEY_BYTES:
                raise ValueError(f"key v{version} must be {KEY_BYTES} bytes, got {len(key)}")
        self._keys = dict(keys)
        self._active = max(self._keys)

    @classmethod
    def from_env(cls, spec: str) -> KeyRing:
        """Parse `"2:<base64>,1:<base64>"` (a leading `v` on versions is accepted)."""
        keys: dict[int, bytes] = {}
        for raw_entry in spec.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            version_str, sep, b64 = entry.partition(":")
            if not sep:
                raise ValueError("ENCRYPTION_KEYS entries must look like '<version>:<base64>'")
            version = int(version_str.strip().lstrip("vV"))
            if version in keys:
                raise ValueError(f"duplicate key version {version}")
            try:
                keys[version] = base64.b64decode(b64.strip(), validate=True)
            except binascii.Error as exc:
                raise ValueError(f"key v{version} is not valid base64") from exc
        return cls(keys)

    @property
    def active_version(self) -> int:
        return self._active

    @property
    def versions(self) -> list[int]:
        return sorted(self._keys)

    def key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise KeyError(f"encryption key v{version} is not configured") from None
