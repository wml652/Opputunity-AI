"""Structured logging with mandatory secret/PII redaction.

Redaction runs as a structlog processor (for our own logs) AND as a `logging.Filter` attached to
the root logger and to noisy third-party loggers (`httpx`, `openai`, `uvicorn`, ...), so a key can
never reach a handler even if a library logs it."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:or-|proj-|ant-)?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/]+=*"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\"',\s}]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
)

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "api_key",
        "embedding_api_key",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "jwt_secret",
        "encryption_keys",
        "cookie",
        "set-cookie",
        "x-csrf-token",
        "csrf_token",
        "phone",
        "phone_number",
        "address",
        "home_address",
        "date_of_birth",
        "dob",
        "national_id",
        "passport_number",
        "storage_state",
        "whatsapp_number",
    }
)


def redact_text(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        event_dict[key] = redact_value(event_dict[key], key)
    return event_dict


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, dict):
            record.msg = redact_value(record.msg)
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed format args; drop them rather than crash
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        return True


THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "openai",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "arq",
    "sqlalchemy.engine",
    "asyncio",
    "fastapi",
)

_configured = False


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _configured
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redaction_processor,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
        root.addFilter(RedactingFilter())
        for name in THIRD_PARTY_LOGGERS:
            logging.getLogger(name).addFilter(RedactingFilter())
        _configured = True
    root.setLevel(level.upper())


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
