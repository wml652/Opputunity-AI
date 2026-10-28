"""Local filesystem object storage for encrypted blobs (swap for S3 in the cloud)."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.bin$")


class LocalFileStorage:
    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError(f"invalid storage key: {key!r}")
        path = (self._base / key).resolve()
        if self._base not in path.parents:
            raise ValueError("storage key escapes the storage root")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)  # atomic on the same volume

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)
