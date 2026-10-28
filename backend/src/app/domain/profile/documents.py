"""Document vault: CV, transcripts, ID. Only real PDFs are accepted (magic-byte check, not the
client-declared MIME type); blobs and extracted text are encrypted at rest."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.shared.enums import DocKind
from app.domain.shared.errors import ValidationError

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
MAX_EXTRACTED_CHARS = 200_000


class FileTooLargeError(ValidationError):
    code = "file_too_large"


class UnsupportedFileError(ValidationError):
    code = "unsupported_file"


def validate_pdf(data: bytes, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> None:
    if not data:
        raise UnsupportedFileError("file is empty")
    if len(data) > max_bytes:
        raise FileTooLargeError(f"file exceeds {max_bytes // (1024 * 1024)} MB")
    if not data.startswith(PDF_MAGIC):
        raise UnsupportedFileError("only PDF files are accepted (missing %PDF- signature)")


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
    return (cleaned or "document.pdf")[:200]


@dataclass(slots=True)
class Document:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: DocKind
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    storage_key: str
    page_count: int | None
    text_chars: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    page_count: int
    encrypted_pdf: bool = False
