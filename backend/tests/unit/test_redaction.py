from __future__ import annotations

import logging

import pytest

from app.infrastructure.logging import (
    REDACTED,
    RedactingFilter,
    redact_text,
    redact_value,
    redaction_processor,
)

NIM_KEY = "nvapi-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
OR_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
OPENAI_KEY = "sk-proj-ABCDEFGHIJKLMNOP1234"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize(
    "text",
    [
        f"Incorrect API key provided: {NIM_KEY}",
        f"key={OR_KEY} rejected",
        f"using {OPENAI_KEY}",
        "Authorization: Bearer abc.def.ghi",
        "headers={'authorization': 'Bearer xyz123'}",
        f"cookie access_token={JWT}",
    ],
)
def test_redact_text_removes_secrets(text: str) -> None:
    out = redact_text(text)
    for secret in (NIM_KEY, OR_KEY, OPENAI_KEY, "abc.def.ghi", "xyz123", JWT):
        assert secret not in out
    assert REDACTED in out


def test_plain_text_untouched() -> None:
    assert redact_text("latency 312 ms for meta/llama-3.3-70b") == (
        "latency 312 ms for meta/llama-3.3-70b"
    )


def test_redact_value_by_key_and_nested() -> None:
    value = {
        "api_key": "anything",
        "nested": {"phone": "+1 555 0100", "note": f"bad {NIM_KEY}"},
        "items": [f"{OR_KEY}", "ok"],
        "model": "x",
    }
    out = redact_value(value)
    assert out["api_key"] == REDACTED
    assert out["nested"]["phone"] == REDACTED
    assert NIM_KEY not in out["nested"]["note"]
    assert OR_KEY not in out["items"][0]
    assert out["model"] == "x"


def test_structlog_processor() -> None:
    event = {"event": f"failed with {NIM_KEY}", "password": "hunter2", "latency_ms": 5}
    out = redaction_processor(None, "info", event)
    assert NIM_KEY not in out["event"]
    assert out["password"] == REDACTED
    assert out["latency_ms"] == 5


def test_logging_filter_redacts_args() -> None:
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "auth %s", (NIM_KEY,), None)
    assert RedactingFilter().filter(record)
    assert NIM_KEY not in record.getMessage()
