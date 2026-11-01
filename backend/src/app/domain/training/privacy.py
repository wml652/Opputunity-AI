"""PII tokenization for agent trajectories and exported training data.

Every personal value is replaced by a symbolic profile token (`{{PROFILE.FULL_NAME}}`,
`{{PROFILE.EMAIL}}`, `{{PROFILE.PHONE}}`...), so a model trained on the data learns WHICH profile
field belongs in WHICH form control - and the dataset contains no one's data and can be
published. Known values are replaced longest-first (case-insensitive); emails, phone numbers,
URLs and ID-like numbers that are not in the profile are replaced by generic `{{PII.*}}` tokens."""

from __future__ import annotations

import re
from collections.abc import Mapping

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_PHONE = re.compile(r"(?<![\w{])\+?\d[\d\s().-]{7,}\d(?![\w}])")
_IDNUM = re.compile(r"(?<![\w{])\d{5}-\d{7}-\d(?![\w}])")  # CNIC-style national ids
TOKEN = re.compile(r"\{\{[A-Z_]+\.[A-Z0-9_]+\}\}")

# Fact keys (application facts / profile) -> token names. Anything else maps to its upper name.
PROFILE_TOKENS = {
    "full_name": "FULL_NAME",
    "first_name": "FIRST_NAME",
    "last_name": "LAST_NAME",
    "email": "EMAIL",
    "phone": "PHONE",
    "home_address": "HOME_ADDRESS",
    "date_of_birth": "DATE_OF_BIRTH",
    "national_id": "NATIONAL_ID",
    "passport_number": "PASSPORT_NUMBER",
}


def profile_token(fact_key: str) -> str:
    name = PROFILE_TOKENS.get(fact_key, re.sub(r"[^A-Z0-9]+", "_", fact_key.upper()).strip("_"))
    return "{{PROFILE." + name + "}}"


def tokenize_pii(text: str, known: Mapping[str, str]) -> str:
    """`known`: fact key -> the student's value. Returns the text with every personal value
    tokenized. Short values (< 3 characters) are ignored to avoid shredding normal words."""
    if not text:
        return text
    out = text
    pairs = sorted(
        ((k, v.strip()) for k, v in known.items() if v and len(v.strip()) >= 3),
        key=lambda kv: -len(kv[1]),
    )
    for key, value in pairs:
        out = re.sub(re.escape(value), profile_token(key), out, flags=re.I)
    out = _EMAIL.sub("{{PII.EMAIL}}", out)
    out = _URL.sub("{{PII.URL}}", out)
    out = _IDNUM.sub("{{PII.ID_NUMBER}}", out)
    out = _PHONE.sub("{{PII.PHONE}}", out)
    return out


def leaks(text: str, known: Mapping[str, str]) -> list[str]:
    """Personal values still present in `text` (the export refuses records with any)."""
    low = text.lower()
    found = [k for k, v in known.items() if v and len(v.strip()) >= 3 and v.strip().lower() in low]
    if _EMAIL.search(text):
        found.append("email-pattern")
    return found
