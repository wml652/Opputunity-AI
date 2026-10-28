"""Provider webhook signature verification (pure stdlib HMAC)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping


def verify_twilio_signature(
    auth_token: str, url: str, params: Mapping[str, str], signature: str
) -> bool:
    """Twilio: base64(HMAC-SHA1(auth_token, url + concat(sorted key + value)))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def verify_meta_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Meta: `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app_secret, raw body)>`."""
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
