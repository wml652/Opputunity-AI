"""Print a new ENCRYPTION_KEYS entry: `uv run python -m app.infrastructure.crypto.keygen [v]`."""

from __future__ import annotations

import base64
import os
import sys

from app.infrastructure.crypto.key_ring import KEY_BYTES


def new_key_entry(version: int = 1) -> str:
    return f"{version}:{base64.b64encode(os.urandom(KEY_BYTES)).decode()}"


if __name__ == "__main__":
    print(new_key_entry(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
