"""Test helpers: a minimal valid PDF writer and a controllable 1024-d embedding provider."""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.domain.llm.embedding import EMBEDDING_DIM, EmbeddingRoute
from app.infrastructure.llm.embeddings import HashingEmbedding1024


def make_pdf(lines: Sequence[str]) -> bytes:
    """Single-page PDF (Helvetica text) with a correct xref table; pypdf extracts the lines."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text_ops = " T* ".join(f"({esc(line)}) Tj" for line in lines)
    stream = f"BT /F1 12 Tf 14 TL 72 720 Td {text_ops} ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def unit(*components: tuple[int, float]) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    for idx, value in components:
        vec[idx] = value
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def at_cosine(similarity: float) -> list[float]:
    """Unit vector whose cosine with e0 is exactly `similarity`."""
    return unit((0, similarity), (1, math.sqrt(max(0.0, 1 - similarity**2))))


class ControlledEmbedding:
    """Exact vectors for registered texts; deterministic hashing for everything else."""

    model_name = "test/controlled-1024"

    def __init__(self, overrides: dict[str, list[float]]) -> None:
        self._overrides = overrides
        self._fallback = HashingEmbedding1024.__new__(HashingEmbedding1024)  # skip warning

    def _one(self, text: str) -> list[float]:
        if text in self._overrides:
            return self._overrides[text]
        return self._fallback._embed(text)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._one(text)


class ControlledEmbeddingFactory:
    def __init__(self) -> None:
        self.overrides: dict[str, list[float]] = {}
        self.calls = 0

    def create(self, route: EmbeddingRoute, api_key: str | None) -> ControlledEmbedding:
        self.calls += 1
        return ControlledEmbedding(self.overrides)
