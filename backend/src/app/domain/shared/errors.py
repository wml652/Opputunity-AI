"""Domain error hierarchy. Adapters translate these into transport-level responses."""

from __future__ import annotations


class DomainError(Exception):
    code: str = "domain_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class ValidationError(DomainError):
    code = "validation_error"


class AuthenticationError(DomainError):
    code = "authentication_failed"


class TokenReuseError(AuthenticationError):
    code = "refresh_token_reuse"


class UnsafeUrlError(ValidationError):
    code = "unsafe_url"


class DnsResolutionError(UnsafeUrlError):
    """The host did not resolve (yet). Still refused (fail closed), but this is a transient
    network condition, not an SSRF violation, and is classified/retried as such."""

    code = "dns_error"


class DecryptionError(DomainError):
    code = "decryption_failed"
