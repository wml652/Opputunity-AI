from __future__ import annotations

import asyncio
import io
import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.domain.profile.documents import MAX_EXTRACTED_CHARS, ExtractedText, UnsupportedFileError

logging.getLogger("pypdf").setLevel(logging.ERROR)  # pypdf warns loudly on minor quirks

MAX_PAGES = 200


class PypdfTextExtractor:
    """Text extraction runs in a worker thread (pypdf is synchronous and CPU-bound)."""

    async def extract(self, data: bytes) -> ExtractedText:
        return await asyncio.to_thread(self._extract, data)

    @staticmethod
    def _extract(data: bytes) -> ExtractedText:
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            encrypted = bool(reader.is_encrypted)
            if encrypted and not reader.decrypt(""):
                return ExtractedText(text="", page_count=0, encrypted_pdf=True)
            pages = reader.pages
            parts: list[str] = []
            total = 0
            for page in list(pages)[:MAX_PAGES]:
                text = page.extract_text() or ""
                parts.append(text)
                total += len(text)
                if total >= MAX_EXTRACTED_CHARS:
                    break
            return ExtractedText(
                text="\n\n".join(p.strip() for p in parts if p.strip())[:MAX_EXTRACTED_CHARS],
                page_count=len(pages),
                encrypted_pdf=encrypted,
            )
        except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
            raise UnsupportedFileError("the PDF could not be read (corrupt or malformed)") from exc
