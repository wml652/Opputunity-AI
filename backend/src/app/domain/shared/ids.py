"""Identifier helpers. UUIDv7 keeps primary keys time-ordered (index-friendly) and lets rows be
created app-side so encryption AAD can include the row id before INSERT."""

from __future__ import annotations

import os
import time
import uuid
from typing import NewType

UserId = NewType("UserId", uuid.UUID)


def new_uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit unix ms timestamp, version/variant bits, 74 random bits."""
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a (12 bits)
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand & ((1 << 62) - 1)  # rand_b (62 bits)
    return uuid.UUID(int=value)
