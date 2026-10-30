"""SSRF guard for every user-influenced outbound URL: custom LLM gateways, crawl targets, search
results, redirects, and every subresource the headless browser requests.

Every resolved address must be public: private, loopback, link-local (incl. cloud metadata
169.254.169.254 / fd00:ec2::254), CGNAT, multicast, reserved, and IPv4-mapped/6to4/Teredo forms of
those are rejected. Validation happens at save time and again before each use (DNS can change).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.domain.shared.errors import DnsResolutionError, UnsafeUrlError

Resolver = Callable[[str, int], Awaitable[list[str]]]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None and not is_public_ip(str(embedded)):
            return False
    if ip in _METADATA:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _ip_literal(host: str) -> str | None:
    candidate = host.strip("[]").split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


class SsrfGuard:
    def __init__(
        self,
        *,
        allow_private: bool = False,
        allow_http: bool = False,
        allowed_ports: Iterable[int] = (80, 443),
        resolver: Resolver | None = None,
        test_fixture_hosts: Iterable[str] = (),
    ) -> None:
        self._allow_private = allow_private
        self._allow_http = allow_http
        self._ports = frozenset(allowed_ports)
        self._resolve = resolver or default_resolver
        # Exact "host:port" pairs that bypass the checks. ONLY wired up in test mode, for local
        # fixture portals served by pytest; settings refuse this outside ENVIRONMENT=test.
        self._fixtures = frozenset(h.lower() for h in test_fixture_hosts)

    async def resolve_pinned(self, host: str, port: int) -> list[str]:
        """Public IPs to connect to for an ALREADY validated host (re-checked here, so a DNS
        answer that changed since `validate` still cannot reach a private address)."""
        host = host.strip("[]").rstrip(".").lower()
        if self._fixtures and f"{host}:{port}" in self._fixtures:
            return [host]
        literal = _ip_literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                addresses = await self._resolve(host, port)
            except OSError as exc:
                raise DnsResolutionError("host does not resolve") from exc
        public = [a for a in addresses if self._allow_private or is_public_ip(a)]
        if not public or len(public) != len(addresses):
            raise UnsafeUrlError("host resolves to a non-public address")
        return public

    async def validate(self, url: str) -> str:
        try:
            parts = urlsplit(url.strip())
        except ValueError as exc:
            raise UnsafeUrlError("malformed URL") from exc
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise UnsafeUrlError("only http(s) URLs are allowed")
        fixture = (
            self._fixtures
            and f"{(parts.hostname or '').lower()}:{parts.port or ''}" in self._fixtures
        )
        if scheme == "http" and not (self._allow_http or self._allow_private or fixture):
            raise UnsafeUrlError("https is required")
        if parts.username or parts.password:
            raise UnsafeUrlError("credentials in URLs are not allowed")
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise UnsafeUrlError("URL has no host")
        if self._fixtures and f"{host}:{parts.port or ''}" in self._fixtures:
            return urlunsplit((scheme, parts.netloc.lower(), parts.path, parts.query, ""))
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise UnsafeUrlError("invalid port") from exc
        if not self._allow_private and port not in self._ports:
            raise UnsafeUrlError(f"port {port} is not allowed")
        if not self._allow_private:
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                raise UnsafeUrlError("internal hostnames are not allowed")
            literal = _ip_literal(host)
            if literal is not None:
                # IP literals are judged directly, never via DNS (a resolver cannot launder them).
                if not is_public_ip(literal):
                    raise UnsafeUrlError("URL points to a non-public address")
                addresses = [literal]
            else:
                addresses = []
            try:
                addresses = addresses or await self._resolve(host, port)
            except OSError as exc:
                raise DnsResolutionError("host does not resolve") from exc
            if not addresses:
                raise DnsResolutionError("host does not resolve")
            bad = [a for a in addresses if not is_public_ip(a)]
            if bad:
                raise UnsafeUrlError("URL resolves to a non-public address")
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]" if parts.port is None else f"[{host}]:{parts.port}"
        return urlunsplit((scheme, netloc, parts.path, parts.query, ""))
