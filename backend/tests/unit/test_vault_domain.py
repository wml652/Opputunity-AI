from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.essay.models import chunk_essay, word_count
from app.domain.memory.models import AUTO_FILL_THRESHOLD, ConfidenceScore, normalize_question
from app.domain.profile.documents import (
    FileTooLargeError,
    UnsupportedFileError,
    safe_filename,
    validate_pdf,
)
from app.domain.profile.models import GPA, StudentPII, clean_skills
from app.domain.shared.errors import ValidationError


@pytest.mark.parametrize(
    ("value", "scale", "norm4", "pct"),
    [
        ("3.7", "4", "3.70", "92.5"),
        ("4.5", "5", "3.60", "90.0"),
        ("8.2", "10", "3.28", "82.0"),
        ("85", "100", "3.40", "85.0"),
    ],
)
def test_gpa_normalization(value: str, scale: str, norm4: str, pct: str) -> None:
    gpa = GPA.of(value, scale)
    assert gpa.normalized_4 == Decimal(norm4)
    assert gpa.percent == Decimal(pct)


@pytest.mark.parametrize(("value", "scale"), [("4.1", "4"), ("-1", "4"), ("3", "0"), ("x", "4")])
def test_gpa_rejects_invalid(value: str, scale: str) -> None:
    with pytest.raises(ValidationError):
        GPA.of(value, scale)


def test_pii_round_trip_and_emptiness() -> None:
    pii = StudentPII(phone="+92 300 0000000", national_id="35202-1234567-1")
    assert StudentPII.from_dict(pii.to_dict()) == pii
    assert not pii.is_empty()
    assert StudentPII().is_empty()


def test_clean_skills_dedupes_case_insensitively() -> None:
    assert clean_skills([" Python ", "python", "", "SQL", "sql "]) == ["Python", "SQL"]


@pytest.mark.parametrize(
    ("score", "auto"), [(0.85, True), (0.8499, False), (0.97, True), (0.2, False)]
)
def test_confidence_threshold(score: float, auto: bool) -> None:
    assert AUTO_FILL_THRESHOLD == 0.85
    assert ConfidenceScore.from_cosine(score).auto_fill is auto


def test_confidence_clamps() -> None:
    assert ConfidenceScore.from_cosine(1.0000002).value == 1.0
    assert ConfidenceScore.from_cosine(-0.3).value == 0.0


def test_normalize_question() -> None:
    assert normalize_question("  What is your   Intended MAJOR?* ") == "what is your intended major"


def test_validate_pdf_magic_bytes_and_size() -> None:
    validate_pdf(b"%PDF-1.7\n...")
    with pytest.raises(UnsupportedFileError):
        validate_pdf(b"\x89PNG\r\n\x1a\n fake pdf")  # PNG renamed to .pdf
    with pytest.raises(UnsupportedFileError):
        validate_pdf(b"<html>%PDF-1.4</html>")  # signature must be at byte 0
    with pytest.raises(UnsupportedFileError):
        validate_pdf(b"")
    with pytest.raises(FileTooLargeError):
        validate_pdf(b"%PDF-" + b"0" * 2048, max_bytes=1024)


def test_safe_filename() -> None:
    assert safe_filename("C:\\Users\\x\\..\\CV<final>.pdf") == "CVfinal.pdf"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("") == "document.pdf"


def test_chunk_essay_respects_paragraphs() -> None:
    para = " ".join(["word"] * 60) + "."
    body = "\n\n".join([para] * 5)  # 5 paragraphs x 60 words
    chunks = chunk_essay(body, target_words=100)
    assert all("\n\n" not in c or word_count(c) <= 150 for c in chunks)
    assert sum(word_count(c) for c in chunks) == 300
    long_para = " ".join(["Sentence number here."] * 100)  # one 300-word paragraph
    split = chunk_essay(long_para, target_words=100)
    assert len(split) >= 3
