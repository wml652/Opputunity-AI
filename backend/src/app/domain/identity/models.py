from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.shared.errors import ValidationError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 320:
        raise ValidationError("invalid email address")
    return email


def validate_password(password: str) -> None:
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise ValidationError(
            f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters"
        )


@dataclass(slots=True)
class User:
    id: uuid.UUID
    email: str
    password_hash: str
    display_name: str
    timezone: str
    is_active: bool
    created_at: datetime


@dataclass(slots=True)
class RefreshToken:
    id: uuid.UUID
    user_id: uuid.UUID
    family_id: uuid.UUID
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by: uuid.UUID | None
    created_at: datetime

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Result of login/refresh: raw tokens leave the system only through the API adapter."""

    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
