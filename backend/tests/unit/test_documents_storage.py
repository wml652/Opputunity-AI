from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.profile.documents import UnsupportedFileError
from app.infrastructure.documents.pdf import PypdfTextExtractor
from app.infrastructure.storage.local_fs import LocalFileStorage
from tests.fixtures import make_pdf


async def test_pdf_text_extraction() -> None:
    pdf = make_pdf(["Curriculum Vitae", "Jane Student - BSc Computer Science"])
    result = await PypdfTextExtractor().extract(pdf)
    assert result.page_count == 1
    assert "Curriculum Vitae" in result.text
    assert "BSc Computer Science" in result.text


async def test_corrupt_pdf_rejected() -> None:
    with pytest.raises(UnsupportedFileError):
        await PypdfTextExtractor().extract(b"%PDF-1.4\nthis is not really a pdf")


async def test_local_storage_round_trip(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    await storage.put("user-1/doc-1.bin", b"\x00secret")
    assert await storage.get("user-1/doc-1.bin") == b"\x00secret"
    await storage.delete("user-1/doc-1.bin")
    await storage.delete("user-1/doc-1.bin")  # idempotent
    assert not (tmp_path / "user-1" / "doc-1.bin").exists()


@pytest.mark.parametrize("key", ["../escape.bin", "a/../../b.bin", "/abs.bin", "x.txt", "a b.bin"])
async def test_local_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        await LocalFileStorage(tmp_path).put(key, b"x")
