"""Opportunity documents published as PDFs (calls for applications, brochures).

Text (and text-based tables) come from pypdf. Scanned / image-only PDFs yield little text; the
caller then reports it in the trace. (Vision parsing applies to rendered HTML pages, whose
screenshots the browser can produce; rasterizing PDF pages would need an extra native library.)"""

from __future__ import annotations

import re

from app.domain.ports.discovery import ParsedDocument
from app.domain.ports.vault import IPdfTextExtractor

_SPACES = re.compile(r"[ \t]{2,}")


class PdfLayoutParser:
    def __init__(self, pdf: IPdfTextExtractor) -> None:
        self._pdf = pdf

    async def parse_pdf(self, data: bytes) -> ParsedDocument:
        extracted = await self._pdf.extract(data)
        lines: list[str] = []
        for raw in extracted.text.splitlines():
            line = raw.rstrip()
            # pypdf renders table rows as runs of cells separated by wide gaps: keep them as
            # pipe-separated rows so the extractor still sees the columns.
            if _SPACES.search(line) and len(_SPACES.split(line.strip())) >= 3:
                line = "| " + " | ".join(_SPACES.split(line.strip())) + " |"
            lines.append(line)
        markdown = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return ParsedDocument(markdown=markdown, pages=extracted.page_count, method="pypdf")
